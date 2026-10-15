"""Local browser-integration fixture using the real API/runtime, not production wiring.

Dangerous is a test-only risk classification to exercise QueueApprover. The
callable only returns an in-memory public text artifact; it performs no action.
"""

import os

from os_core.hitl import QueueApprover
from os_core.models import ModelResponse, ScriptedModel
from os_core.types import Artifact, RiskLevel, ToolCall, ToolResult, ToolStatus
from os_orchestration.platform import Platform, PlatformConfig
from os_server.app import create_app
from os_tools.registry import Tool, ToolRegistry


def factory(mode: str, max_steps: int) -> Platform:
    def responder(messages):
        if not any(m.role == "tool" for m in messages):
            return ModelResponse(tool_calls=[ToolCall(name="test_approval_only", args={})])
        return ModelResponse(
            content="Local test fixture finished. Goal not independently verified."
        )

    def fixture_result():
        return ToolResult(
            tool_call_id="",
            tool_name="test_approval_only",
            status=ToolStatus.SUCCESS,
            output="Public in-memory fixture only.",
            artifacts=[
                Artifact(kind="file", filename="approval-fixture.txt", content="Public fixture.")
            ],
        )

    registry = ToolRegistry()
    registry.add(
        Tool(
            name="test_approval_only",
            description="No-op test fixture; no external effects",
            parameters={"type": "object", "properties": {}},
            risk=RiskLevel.DANGEROUS,
            fn=fixture_result,
        )
    )
    return Platform(
        model=ScriptedModel(name="ui-test-fixture", responder=responder),
        registry=registry,
        config=PlatformConfig(mode="react", max_steps=max_steps),
        approver=QueueApprover(timeout=60),
    )


if __name__ == "__main__":
    import uvicorn

    os.environ["CELA_LEDGER_PATH"] = "off"
    os.environ["CELA_RUN_STORE_URL"] = "memory://"
    uvicorn.run(
        create_app(factory, api_token=os.environ["CELA_API_TOKEN"], store_url="memory://"),
        host="0.0.0.0",
        port=8091,
        access_log=False,
        log_level="warning",
    )
