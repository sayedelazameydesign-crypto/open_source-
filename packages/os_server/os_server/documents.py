"""MVP-0: bounded, offline text processing. Single-process execution only.

Document state uses the existing RunStore; source text is private state metadata,
not an artifact, log, model prompt or long-term memory. One process-wide lock
serializes execute requests, including separate app instances in that process.
Multi-worker execution is deliberately unsupported (no distributed claim).
"""

from __future__ import annotations

import hashlib
import re
import threading
import unicodedata
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from os_core.state import RunState, RunStatus

from os_server.storage import RunStore

MAX_BYTES = 1_048_576
_LOCK = threading.RLock()
_PLAN = ["فحص النص وإحصاؤه محليًا", "إنشاء تقرير Markdown", "التحقق وحفظ الناتج"]
_ID = re.compile(r"doc_[0-9a-f]{32}\Z")


def render_report(state: RunState) -> bytes:
    """No model, network, shell, HTML rendering or interpretation of input."""
    text = state.metadata["source"]
    # An indented code block keeps Markdown/HTML in the excerpt inert in renderers.
    excerpt = "\n".join("    " + line for line in text[:500].splitlines())
    return (
        f"# تقرير معالجة نص محلية\n\nالمهمة: {state.run_id}\nالحالة: SUCCEEDED\n"
        f"وقت الإنشاء: {state.started_at}\n"
        f"الحجم بالبايت: {state.metadata['size_bytes']}\n"
        f"الأحرف: {len(text)}\nالكلمات: {len(text.split())}\n"
        f"الأسطر: {len(text.splitlines())}\nالأخطاء: لا يوجد\n\n"
        f"## مقتطف (حتى 500 نقطة Unicode)\n\n{excerpt}\n\n"
        "هذه معالجة حتمية وليست تلخيصًا ذكيًا.\n"
    ).encode()


class DocumentRuns:
    def __init__(self, store: RunStore):
        self.store = store

    def load(self, run_id: str) -> RunState:
        if not _ID.fullmatch(run_id):
            raise HTTPException(404, "document run not found")
        state = self.store.get_state(run_id)
        if state is None or state.mode != "document":
            raise HTTPException(404, "document run not found")
        return state

    def public(self, state: RunState) -> dict:
        data = state.metadata
        return {
            "run_id": state.run_id,
            "status": data["document_status"],
            "plan": _PLAN,
            "created_at": state.started_at,
            "events": data["events"],
            "error": state.error,
            "artifact": data.get("artifact"),
            "statistics": data.get("statistics"),
        }

    def transition(self, state: RunState, status: str) -> None:
        state.metadata["document_status"] = status
        state.metadata["events"].append({"status": status, "at": datetime.now(UTC).isoformat()})
        self.store.save_state(state)

    def create(self, filename: str, raw: bytes) -> dict:
        # MIME is not evidence of text. Filename is validation-only, never a path.
        if not filename.lower().endswith(".txt"):
            raise HTTPException(415, "only .txt files are accepted")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise HTTPException(422, "file must be valid UTF-8") from None
        if any(unicodedata.category(c) == "Cc" and c not in "\t\r\n" for c in text):
            raise HTTPException(422, "binary/control characters are not accepted")
        state = RunState(
            run_id=f"doc_{uuid.uuid4().hex}",
            mode="document",
            status=RunStatus.PENDING,
            metadata={"source": text, "size_bytes": len(raw), "events": []},
        )
        with _LOCK:
            self.transition(state, "RECEIVED")
            self.transition(state, "PLAN_READY")
            self.transition(state, "AWAITING_CONFIRMATION")
        return self.public(state)

    def execute(self, run_id: str) -> dict:
        with _LOCK:
            state = self.load(run_id)
            status = state.metadata["document_status"]
            if status in {"SUCCEEDED", "RUNNING"}:
                return self.public(state)
            if status != "AWAITING_CONFIRMATION":
                raise HTTPException(409, "document run cannot execute in this state")
            state.status = RunStatus.RUNNING
            self.transition(state, "RUNNING")
            try:
                content = render_report(state)
                if not content or not content.decode("utf-8").startswith("# "):
                    raise ValueError("invalid report")
                artifact_id = f"report_{uuid.uuid4().hex}.md"
                artifact = {
                    "artifact_id": artifact_id,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "mime_type": "text/markdown; charset=utf-8",
                }
                self.store.save_artifact(
                    run_id, artifact_id, {**artifact, "content": content.decode("utf-8")}
                )
                text = state.metadata["source"]
                state.metadata["statistics"] = {
                    "bytes": state.metadata["size_bytes"],
                    "characters": len(text),
                    "words": len(text.split()),
                    "lines": len(text.splitlines()),
                }
                state.metadata["artifact"] = artifact
                state.status = RunStatus.COMPLETED
                state.metadata.pop("source", None)
                self.transition(state, "SUCCEEDED")
            except Exception:
                # Never persist exception messages: parsers/storage may include input.
                state.status = RunStatus.FAILED
                state.error = "document processing failed"
                state.metadata.pop("source", None)
                state.metadata.pop("artifact", None)
                self.transition(state, "FAILED")
            return self.public(state)


def document_router(store: RunStore, require_auth) -> APIRouter:
    service = DocumentRuns(store)
    router = APIRouter(prefix="/document-runs", dependencies=[Depends(require_auth)])

    @router.post("", status_code=201)
    async def create(request: Request, filename: str) -> dict:
        # Raw file body avoids a multipart parser/dependency and bounds bytes read.
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_BYTES:
                raise HTTPException(413, "file exceeds 1048576 bytes")
            raw.extend(chunk)
        return service.create(filename, bytes(raw))

    @router.get("/{run_id}")
    def get(run_id: str) -> dict:
        return service.public(service.load(run_id))

    @router.post("/{run_id}/execute")
    def execute(run_id: str) -> dict:
        return service.execute(run_id)

    @router.get("/{run_id}/artifacts/{artifact_id}/download")
    def download(run_id: str, artifact_id: str) -> Response:
        state = service.load(run_id)
        meta = state.metadata.get("artifact")
        if (
            state.metadata["document_status"] != "SUCCEEDED"
            or not meta
            or meta["artifact_id"] != artifact_id
        ):
            raise HTTPException(404, "artifact not found")
        artifact = store.get_artifact(run_id, artifact_id)
        if artifact is None:
            raise HTTPException(404, "artifact not found")
        content = artifact["content"].encode("utf-8")
        if hashlib.sha256(content).hexdigest() != meta["sha256"]:
            raise HTTPException(409, "artifact integrity check failed")
        return Response(
            content,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{artifact_id}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router
