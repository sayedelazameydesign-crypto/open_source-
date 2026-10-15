"""OTLP/HTTP export — our ledger and event stream, readable by anybody's dashboard.

Two decisions in this file:

**Export the standard, not a vendor SDK.** The platform already emits a durable
per-run outcome (:class:`~os_core.ledger.RunLedger`) and sequence-numbered events
(:class:`~os_core.events.EventBus`); a vendor's tracing SDK would have to be
told the same facts again, and would then own the schema. OTLP/HTTP is what
Langfuse, Arize Phoenix, Grafana Tempo and Jaeger all ingest, so one exporter
serves all of them. JSON encoding is used deliberately: the spec allows it, and
it keeps the dependency count at zero — no ``opentelemetry-*``, no protobuf.

**The destination is operator configuration, never model input.** Read from
``CELA_OTEL_ENDPOINT`` at call time, so a loopback collector is reachable (the
:class:`~os_tools.ssrf.SSRFGuard` floor would otherwise refuse it — correctly, for
a URL an agent chose) while nothing an agent says can redirect telemetry.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from os_core.redact import scrub_credentials
from os_core.state import RunState
from pydantic import BaseModel, Field

ENV_ENDPOINT = "CELA_OTEL_ENDPOINT"
ENV_SERVICE = "CELA_OTEL_SERVICE"
#: Attribute values are capped: an answer can be long, a span attribute should be
#: a label. Collectors reject oversized payloads and the run must not pay for it.
MAX_ATTR_CHARS = 512
MAX_SPANS = 512

_STATUS_OK = 1
_STATUS_ERROR = 2
_SPAN_KIND_INTERNAL = 1
_SPAN_KIND_SERVER = 2


def trace_id_for(run_id: str) -> str:
    """A stable 16-byte hex trace id — re-exporting a run joins the same trace."""
    return sha256(f"cela-trace:{run_id}".encode()).hexdigest()[:32]


def span_id_for(*parts: object) -> str:
    return sha256(":".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _nano(moment: object) -> str:
    """Seconds (float/str ISO) → nanoseconds as a decimal string, per OTLP JSON."""
    if moment is None:
        return ""
    if isinstance(moment, (int, float)):
        return str(int(float(moment) * 1e9))
    text = str(moment)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return str(int(parsed.timestamp() * 1e9))


def _attr(key: str, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    text = scrub_credentials(str(value))
    if not text:
        return None
    if len(text) > MAX_ATTR_CHARS:
        text = text[:MAX_ATTR_CHARS] + "…"
    return {"key": key, "value": {"stringValue": text}}


def _attrs(pairs: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for key, value in pairs.items():
        built = _attr(key, value)
        if built is not None:
            out.append(built)
    return out


def spans_for_run(state: RunState, events: list[Any] | None = None) -> list[dict[str, Any]]:
    """One root span for the run, one per step, one per tool call.

    Timings come from the *event stream* (steps carry no timestamps in
    :class:`~os_core.state.RunState`), so a step is measured by when the bus said
    it started and finished — the same numbers the UI renders, not a second
    instrument that can disagree with the first.
    """
    trace_id = trace_id_for(state.run_id)
    root_id = span_id_for("run", state.run_id)
    started = _nano(state.started_at)
    finished = _nano(state.finished_at) or started
    root_attributes = _attrs(
        {
            "cela.run_id": state.run_id,
            "cela.mode": state.mode,
            "cela.status": state.status.value,
            "cela.steps_used": state.steps_used,
            "cela.tokens_used": state.tokens_used,
            "cela.replans": state.replans,
            "cela.reviews": state.reviews,
            "cela.error": state.error,
            "cela.skill_id": state.metadata.get("skill_id"),
            "cela.memory_ids": ",".join(str(m) for m in (state.metadata.get("memory_ids") or []))
            or None,
            "gen_ai.prompt": state.task,
            "gen_ai.completion": state.final_answer,
        }
    )
    spans: list[dict[str, Any]] = [
        {
            "traceId": trace_id,
            "spanId": root_id,
            "name": f"run {state.mode}",
            "kind": _SPAN_KIND_SERVER,
            "startTimeUnixNano": started,
            "endTimeUnixNano": finished,
            "attributes": root_attributes,
            "status": {
                "code": _STATUS_OK if state.status.value == "completed" else _STATUS_ERROR,
                **(
                    {"message": scrub_credentials(str(state.error))[:MAX_ATTR_CHARS]}
                    if state.error
                    else {}
                ),
            },
        }
    ]
    if not events:
        return spans

    open_steps: dict[str, dict[str, Any]] = {}
    open_tools: dict[str, dict[str, Any]] = {}
    for event in events[: MAX_SPANS * 4]:
        data = getattr(event, "data", None) or {}
        kind = str(getattr(event, "type", "") or "")
        stamp = _nano(getattr(event, "ts", 0.0))
        if kind == "step_started":
            step_id = str(data.get("step_id") or data.get("id") or len(open_steps))
            span = {
                "traceId": trace_id,
                "spanId": span_id_for("step", state.run_id, step_id),
                "parentSpanId": root_id,
                "name": f"step {data.get('title') or step_id}",
                "kind": _SPAN_KIND_INTERNAL,
                "startTimeUnixNano": stamp,
                "endTimeUnixNano": stamp,
                "attributes": _attrs({"cela.step_id": step_id, "cela.step_tool": data.get("tool")}),
                "status": {"code": _STATUS_OK},
            }
            open_steps[step_id] = span
            spans.append(span)
        elif kind == "step_finished":
            step_id = str(data.get("step_id") or data.get("id") or "")
            span = open_steps.get(step_id)
            if span is not None:
                span["endTimeUnixNano"] = stamp
                status = str(data.get("status") or "")
                span["status"] = {
                    "code": _STATUS_ERROR if status in {"failed", "error"} else _STATUS_OK,
                    **(
                        {"message": scrub_credentials(str(data.get("error")))[:256]}
                        if data.get("error")
                        else {}
                    ),
                }
                span["attributes"].extend(_attrs({"cela.step_status": status}))
        elif kind == "tool_call":
            call_id = str(data.get("tool_call_id") or data.get("id") or len(open_tools))
            span = {
                "traceId": trace_id,
                "spanId": span_id_for("tool", state.run_id, call_id),
                "parentSpanId": root_id,
                "name": f"tool {data.get('tool') or data.get('name') or call_id}",
                "kind": _SPAN_KIND_INTERNAL,
                "startTimeUnixNano": stamp,
                "endTimeUnixNano": stamp,
                "attributes": _attrs(
                    {
                        "cela.tool": data.get("tool") or data.get("name"),
                        "cela.tool_call_id": call_id,
                        "cela.risk": data.get("risk"),
                    }
                ),
                "status": {"code": _STATUS_OK},
            }
            open_tools[call_id] = span
            spans.append(span)
        elif kind == "tool_result":
            call_id = str(data.get("tool_call_id") or data.get("id") or "")
            span = open_tools.get(call_id)
            if span is not None:
                span["endTimeUnixNano"] = stamp
                span["attributes"].extend(
                    _attrs(
                        {
                            "cela.tool_status": data.get("status"),
                            "cela.duration_ms": data.get("duration_ms"),
                        }
                    )
                )
    return spans[:MAX_SPANS]


def payload_for_run(
    state: RunState, events: list[Any] | None = None, *, service_name: str = "cela_use_browser"
) -> dict[str, Any]:
    """The OTLP/JSON body: ``{"resourceSpans": [...]}``."""
    spans = spans_for_run(state, events)
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": _attrs(
                        {"service.name": service_name, "cela.run_mode": state.mode}
                    )
                },
                "scopeSpans": [
                    {"scope": {"name": "os_orchestration", "version": "0.1.0"}, "spans": spans}
                ],
            }
        ]
    }


class ExportReport(BaseModel):
    """What happened to one export. A failure here never reaches the run."""

    ok: bool
    spans: int = 0
    status_code: int | None = None
    error: str | None = None
    bytes_sent: int = 0

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class OtlpJsonExporter(BaseModel):
    """POSTs OTLP/JSON to an operator-configured collector. Optional by design.

    ``transport`` exists for tests and for deployments that terminate OTLP
    through a proxy: it is handed straight to ``httpx.AsyncClient`` and defaults
    to the real network stack, so the production path is the tested path.
    """

    endpoint: str
    service_name: str = "cela_use_browser"
    timeout: float = 5.0
    headers: dict[str, str] = Field(default_factory=dict)
    transport: Any = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> OtlpJsonExporter | None:
        """Build only when an operator said so. No endpoint, no export, no prompt."""
        source = env if env is not None else os.environ
        endpoint = str(source.get(ENV_ENDPOINT) or "").strip()
        if not endpoint:
            return None
        lowered = endpoint.lower()
        if not lowered.startswith(("http://", "https://")):
            # A bare host, a file path or a `unix://` socket is a configuration
            # mistake; guessing the scheme would silently ship telemetry somewhere
            # the operator did not name.
            raise ValueError(f"{ENV_ENDPOINT} must start with http:// or https://")
        return cls(
            endpoint=endpoint.rstrip("/"),
            service_name=str(source.get(ENV_SERVICE) or "cela_use_browser"),
        )

    @property
    def traces_url(self) -> str:
        return f"{self.endpoint}/v1/traces"

    async def export(self, payload: dict[str, Any]) -> ExportReport:
        spans = len(
            ((payload.get("resourceSpans") or [{}])[0]).get("scopeSpans", [{}])[0].get("spans", [])
        )
        try:
            import httpx

            client_kwargs: dict[str, Any] = {"timeout": self.timeout}
            if self.transport is not None:
                client_kwargs["transport"] = self.transport
            body = json.dumps(payload)
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.post(
                    self.traces_url,
                    content=body.encode("utf-8"),
                    headers={
                        "content-type": "application/json",
                        **self.headers,
                    },
                )
        except Exception as exc:  # telemetry is a reporter, not a dependency
            return ExportReport(ok=False, spans=spans, error=f"{type(exc).__name__}: {exc}")
        code = getattr(response, "status_code", None)
        ok = code is not None and 200 <= int(code) < 300
        return ExportReport(
            ok=ok,
            spans=spans,
            status_code=code,
            bytes_sent=len(body),
            error=None
            if ok
            else scrub_credentials(getattr(response, "text", "") or "")[:256] or None,
        )

    def write_file(self, payload: dict[str, Any], path: str | Path) -> int:
        """Dump the payload instead of sending it — the offline, reviewable path."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        target.write_text(text + "\n", encoding="utf-8")
        return len(text)


__all__ = [
    "ENV_ENDPOINT",
    "ENV_SERVICE",
    "ExportReport",
    "OtlpJsonExporter",
    "payload_for_run",
    "span_id_for",
    "spans_for_run",
    "trace_id_for",
]
