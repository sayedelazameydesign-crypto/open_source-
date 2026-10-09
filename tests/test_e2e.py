"""End-to-end: the three engines wired together through the Platform facade."""

from __future__ import annotations

from os_core.events import EventType
from os_core.hitl import ApprovalDecision, QueueApprover
from os_core.state import RunStatus
from os_orchestration.platform import build_platform
from os_server.demo import build_demo_platform, build_registry


def test_auto_mode_picks_flow_for_planning_tasks() -> None:
    platform = build_demo_platform(mode="auto")
    assert platform.choose_mode("Plan a multi-step research comparison") == "flow"
    assert platform.choose_mode("what time is it") == "react"


def test_explicit_mode_is_respected() -> None:
    assert build_demo_platform(mode="react").choose_mode("Plan a multi-step thing") == "react"
    assert build_demo_platform(mode="flow").choose_mode("hi") == "flow"


async def test_react_end_to_end_with_real_sandbox() -> None:
    platform = build_demo_platform(mode="react")
    bus = platform.make_bus("run_react")
    run = await platform.run("Sum 0..100", run_id="run_react", bus=bus)
    assert run.status is RunStatus.COMPLETED
    assert "5050" in run.final_answer
    assert any(r.ok and "5050" in r.output for r in run.results)


async def test_flow_end_to_end_runs_parallel_branches() -> None:
    platform = build_demo_platform(mode="flow")
    bus = platform.make_bus("run_flow")
    run = await platform.run("Execute the plan", run_id="run_flow", bus=bus)
    assert run.status is RunStatus.COMPLETED
    assert len(run.steps) == 3
    assert run.artifacts, "the browser step should have produced a screenshot artifact"


async def test_browser_fallback_exercised_end_to_end() -> None:
    """The demo site mutates its DOM, so the click must survive via fallback."""
    from os_server.demo import demo_browser

    browser = demo_browser()
    await browser.navigate("https://example.org/search")
    result = await browser.act("click", "#go")
    assert result.ok
    assert browser.history[-1]["strategy"] in {"css", "text", "vision"}


async def test_event_stream_is_replayable_for_a_late_ui() -> None:
    platform = build_demo_platform(mode="react")
    bus = platform.make_bus("run_replay")
    await platform.run("Sum 0..100", run_id="run_replay", bus=bus)
    replayed = bus.replay()
    assert replayed[0].type is EventType.RUN_STARTED
    assert replayed[-1].type is EventType.RUN_FINISHED
    assert all(e.run_id == "run_replay" for e in replayed)


async def test_snapshot_is_json_serialisable() -> None:
    import json

    platform = build_demo_platform(mode="flow")
    run = await platform.run("Execute the plan", run_id="run_snap")
    snapshot = run.snapshot()
    assert json.loads(json.dumps(snapshot))["run_id"] == "run_snap"
    assert {s["status"] for s in snapshot["steps"]} == {"done"}


async def test_dangerous_action_blocks_on_human_approval(tool_response) -> None:
    """A write-classified tool with a DenyApprover must never execute."""
    from os_core.hitl import DenyApprover
    from os_core.models import ScriptedModel
    from os_core.types import RiskLevel, ToolCall

    registry, _guard, _browser = build_registry()
    registry.tools["python_execute"].risk = RiskLevel.DANGEROUS
    model = ScriptedModel(
        script=[tool_response(ToolCall(name="python_execute", args={"code": "print(1)"}))],
        final_content="done",
    )
    platform = build_platform(model, registry=registry, mode="react", approver=DenyApprover())
    run = await platform.run("run some code", run_id="run_hitl")
    assert any(r.status.value == "denied" for r in run.results)


async def test_queue_approver_times_out_closed() -> None:
    approver = QueueApprover(timeout=0.05)
    from os_core.types import RiskLevel, ToolCall

    decision = await approver.request(
        ToolCall(name="send_email", args={}), RiskLevel.DANGEROUS, "money"
    )
    assert decision.value == "denied"
    assert "timed out" in approver.history[0].detail


async def test_queue_approver_can_be_answered() -> None:
    import asyncio

    from os_core.types import RiskLevel, ToolCall

    approver = QueueApprover(timeout=5.0)
    call = ToolCall(name="publish", args={})
    task = asyncio.create_task(approver.request(call, RiskLevel.DANGEROUS, "publish"))
    for _ in range(50):
        if approver.pending:
            break
        await asyncio.sleep(0.01)
    assert approver.answer(call.id, ApprovalDecision.APPROVED)
    assert (await task).value == "approved"


def test_registry_exposes_mcp_style_specs() -> None:
    from os_tools.mcp import LocalMCPServer

    registry, _guard, _browser = build_registry()
    server = LocalMCPServer(registry=registry)
    import asyncio

    specs = asyncio.run(server.list_tools())
    names = {s["function"]["name"] for s in specs}
    assert {"python_execute", "browser_navigate", "browser_click", "browser_fill"} <= names
    assert server.describe()["tools"] == registry.names()


async def test_mcp_call_tool_round_trip() -> None:
    from os_tools.mcp import LocalMCPServer

    registry, _guard, _browser = build_registry()
    server = LocalMCPServer(registry=registry)
    result = await server.call_tool("python_execute", {"code": "print('mcp ok')"})
    assert result.ok
    assert "mcp ok" in result.output
