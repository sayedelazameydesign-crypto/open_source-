"""OTLP/JSON export — the run ledger and the event stream, in a language others read.

Three properties are worth more here than coverage of every attribute: an
unconfigured platform touches **nothing** on the network; a collector that is down
never damages the run it was watching; and no secret rides along in a span.
"""

from __future__ import annotations

import json

import pytest
from os_core.events import Event, EventBus, EventType
from os_core.state import RunState, RunStatus
from os_core.types import ToolResult
from os_server.otel import (
    OtlpJsonExporter,
    payload_for_run,
    span_id_for,
    spans_for_run,
    trace_id_for,
)

httpx = pytest.importorskip("httpx")


def finished_state() -> RunState:
    state = RunState(task="sum 0..100", mode="react")
    state.results.append(
        ToolResult(tool_call_id="c1", tool_name="python_execute", status="success", output="5050")
    )
    state.mark_finished(RunStatus.COMPLETED, answer="the sandbox returned 5050")
    return state


async def collected_bus(state: RunState) -> EventBus:
    bus = EventBus(run_id=state.run_id)
    await bus.publish(
        Event(type=EventType.RUN_STARTED, run_id=state.run_id, data={"task": state.task})
    )
    await bus.publish(
        Event(
            type=EventType.STEP_STARTED,
            run_id=state.run_id,
            data={"step_id": "s1", "title": "compute"},
        )
    )
    await bus.publish(
        Event(
            type=EventType.TOOL_CALL,
            run_id=state.run_id,
            data={"tool_call_id": "c1", "tool": "python_execute", "risk": "write"},
        )
    )
    await bus.publish(
        Event(
            type=EventType.TOOL_RESULT,
            run_id=state.run_id,
            data={"tool_call_id": "c1", "status": "success", "duration_ms": 12.5},
        )
    )
    await bus.publish(
        Event(
            type=EventType.STEP_FINISHED,
            run_id=state.run_id,
            data={"step_id": "s1", "status": "done"},
        )
    )
    return bus


# ------------------------------------------------------------------ shape --
def test_a_run_becomes_one_root_span_with_a_stable_trace_id() -> None:
    state = finished_state()
    spans = spans_for_run(state)
    assert len(spans) == 1
    root = spans[0]
    assert root["name"] == "run react"
    assert root["status"]["code"] == 1
    assert len(root["traceId"]) == 32 and len(root["spanId"]) == 16
    assert int(root["startTimeUnixNano"]) <= int(root["endTimeUnixNano"])
    assert trace_id_for(state.run_id) == root["traceId"]


def test_ids_are_derived_not_random_so_re_exporting_joins_the_same_trace() -> None:
    assert trace_id_for("run_x") == trace_id_for("run_x")
    assert trace_id_for("run_x") != trace_id_for("run_y")
    assert span_id_for("step", "run_x", "s1") == span_id_for("step", "run_x", "s1")


async def test_step_and_tool_spans_pair_up_on_events() -> None:
    state = finished_state()
    bus = await collected_bus(state)
    spans = spans_for_run(state, bus.buffer)
    by_name = {s["name"]: s for s in spans}
    assert any(name.startswith("step compute") for name in by_name)
    tool_span = next(s for s in spans if s["name"] == "tool python_execute")
    root = next(s for s in spans if s["name"] == "run react")
    assert tool_span["parentSpanId"] == root["spanId"]
    keys = {attr["key"] for attr in tool_span["attributes"]}
    assert {"cela.tool", "cela.tool_status", "cela.risk"} <= keys
    # start/end came from the two events, so the duration is the one the UI showed
    assert int(tool_span["endTimeUnixNano"]) > int(tool_span["startTimeUnixNano"])


async def test_a_failed_step_marks_its_span_as_errored() -> None:
    state = finished_state()
    bus = EventBus(run_id=state.run_id)
    await bus.publish(
        Event(
            type=EventType.STEP_STARTED,
            run_id=state.run_id,
            data={"step_id": "s1", "title": "risky"},
        )
    )
    await bus.publish(
        Event(
            type=EventType.STEP_FINISHED,
            run_id=state.run_id,
            data={"step_id": "s1", "status": "failed", "error": "sandbox refused"},
        )
    )
    step = next(s for s in spans_for_run(state, bus.buffer) if s["name"].startswith("step risky"))
    assert step["status"]["code"] == 2
    assert step["status"]["message"] == "sandbox refused"


def test_payload_is_otlp_json_resource_spans() -> None:
    payload = payload_for_run(finished_state(), service_name="cela-test")
    assert set(payload) == {"resourceSpans"}
    resource = payload["resourceSpans"][0]
    assert resource["resource"]["attributes"][0]["key"] == "service.name"
    assert resource["scopeSpans"][0]["scope"]["name"] == "os_orchestration"
    assert resource["scopeSpans"][0]["spans"]
    # JSON encoding keeps ids as hex strings, per the OTLP/Jaeger-compatible spec.
    span = resource["scopeSpans"][0]["spans"][0]
    assert isinstance(span["traceId"], str) and isinstance(span["startTimeUnixNano"], str)


def test_attribute_values_are_typed_and_capped() -> None:
    state = finished_state()
    state.final_answer = "x" * 4000
    attributes = {a["key"]: a["value"] for a in spans_for_run(state)[0]["attributes"]}
    assert "stringValue" in attributes["gen_ai.completion"]
    assert len(attributes["gen_ai.completion"]["stringValue"]) <= 513
    assert (
        attributes["cela.tokens_used"] == {"intValue": "0"}
        or "intValue" in attributes["cela.tokens_used"]
    )
    assert isinstance(attributes.get("cela.replans", {}).get("intValue"), str)


def test_run_identity_and_skill_and_memory_travel_as_attributes() -> None:
    state = finished_state()
    state.metadata["skill_id"] = "skw_abc"
    state.metadata["memory_ids"] = ["mem_1", "mem_2"]
    attributes = {
        a["key"]: next(iter(a["value"].values())) for a in spans_for_run(state)[0]["attributes"]
    }
    assert attributes["cela.run_id"] == state.run_id
    assert attributes["cela.skill_id"] == "skw_abc"
    assert attributes["cela.memory_ids"] == "mem_1,mem_2"


def test_secrets_are_scrubbed_before_they_leave_the_process() -> None:
    state = finished_state()
    state.error = (
        "gateway 401 for https://user:hunter2@api.example.com/v1?api_key=sk-reallysecret123"
    )
    blob = json.dumps(spans_for_run(state))
    assert "hunter2" not in blob
    assert "sk-reallysecret123" not in blob


def test_span_count_is_bounded_by_a_hard_cap() -> None:
    state = finished_state()
    flood = [
        Event(type=EventType.STEP_STARTED, run_id=state.run_id, data={"step_id": f"s{i}"})
        for i in range(5000)
    ]
    spans = spans_for_run(state, flood)
    assert len(spans) <= 512, "a chatty run must not mint an unbounded payload"


# --------------------------------------------------------------- opt-in --
def test_no_endpoint_means_no_exporter_and_no_network() -> None:
    assert OtlpJsonExporter.from_env({}) is None
    assert OtlpJsonExporter.from_env({"CELA_OTEL_ENDPOINT": "   "}) is None


def test_a_schemeless_endpoint_is_a_configuration_error_not_a_guess() -> None:
    with pytest.raises(ValueError, match="must start with"):
        OtlpJsonExporter.from_env({"CELA_OTEL_ENDPOINT": "127.0.0.1:4318"})
    with pytest.raises(ValueError, match="must start with"):
        OtlpJsonExporter.from_env({"CELA_OTEL_ENDPOINT": "unix:/run/otel.sock"})


def test_service_name_is_configurable_and_the_trailing_slash_is_ignored() -> None:
    exporter = OtlpJsonExporter.from_env(
        {"CELA_OTEL_ENDPOINT": "http://collector:4318/", "CELA_OTEL_SERVICE": "cela-prod"}
    )
    assert exporter is not None
    assert exporter.service_name == "cela-prod"
    assert exporter.traces_url == "http://collector:4318/v1/traces"


# ------------------------------------------------------------- transport --
async def test_export_posts_json_to_the_traces_endpoint() -> None:
    seen: dict[str, object] = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(202, json={"partialSuccess": {}})

    exporter = OtlpJsonExporter(
        endpoint="http://127.0.0.1:4318",
        transport=httpx.MockTransport(handler),
        headers={"x-api-key": "k"},
    )
    report = await exporter.export(payload_for_run(finished_state()))
    assert report.ok and report.spans == 1 and report.status_code == 202 and report.bytes_sent > 0
    assert seen["url"].endswith("/v1/traces")
    assert seen["content_type"] == "application/json"
    assert "resourceSpans" in seen["body"]


async def test_a_dead_collector_is_reported_not_raised() -> None:
    def handler(request):
        return httpx.Response(503, text="queue full")

    exporter = OtlpJsonExporter(
        endpoint="http://127.0.0.1:4318", transport=httpx.MockTransport(handler)
    )
    report = await exporter.export(payload_for_run(finished_state()))
    assert not report.ok and report.status_code == 503
    assert report.error and "queue full" in report.error


async def test_a_connection_refused_is_a_report_too() -> None:
    def handler(request):
        raise httpx.ConnectError("connection refused")

    exporter = OtlpJsonExporter(
        endpoint="http://127.0.0.1:1", transport=httpx.MockTransport(handler)
    )
    report = await exporter.export(payload_for_run(finished_state()))
    assert not report.ok
    assert "ConnectError" in (report.error or "")


async def test_error_bodies_are_scrubbed_before_becoming_report_text() -> None:
    def handler(request):
        return httpx.Response(401, text="bad token abcdef123456 for https://u:pw@host/x")

    exporter = OtlpJsonExporter(
        endpoint="http://127.0.0.1:4318", transport=httpx.MockTransport(handler)
    )
    report = await exporter.export(payload_for_run(finished_state()))
    assert "pw@host" not in (report.error or "") or "REDACTED" in (report.error or "")


def test_offline_dump_is_reviewable_json(tmp_path) -> None:
    exporter = OtlpJsonExporter(endpoint="http://unused")
    written = exporter.write_file(payload_for_run(finished_state()), tmp_path / "trace.json")
    text = (tmp_path / "trace.json").read_text(encoding="utf-8")
    assert written == len(text.rstrip())
    assert json.loads(text)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "run react"
