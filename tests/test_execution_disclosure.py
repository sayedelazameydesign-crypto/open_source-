"""Phase UI-0 regressions: configured routing is not model execution evidence."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from os_core.models import OpenAICompatModel, ScriptedModel, describe_execution
from os_core.state import RunState, RunStatus
from os_core.types import Message
from os_server.app import create_app
from os_server.cli import main
from os_server.demo import build_demo_platform, build_demo_platform_for, demo_react_model
from os_server.runtime import ApiRuntime


def test_cli_serve_uses_fresh_mode_specific_platforms(monkeypatch):
    captured = []
    monkeypatch.setenv("CELA_API_TOKEN", "test-only-token")
    monkeypatch.setattr("os_server.app.serve", lambda factory, **kwargs: captured.append(factory))
    assert main(["serve", "--demo"]) == 0
    factory = captured[0]
    assert callable(factory), "passing one planner Platform recreates the ReAct JSON bug"
    react = factory("react", 4)
    flow = factory("flow", 7)
    assert react is not flow
    assert react.model.name == "demo-react"
    assert flow.model.name == "demo-planner"
    assert react.config.max_steps == 4
    assert flow.config.max_steps == 7
    assert factory("react", 2).model is not react.model


async def test_reproduce_original_shared_planner_failure_without_hiding_json():
    # This is the old wiring: mode changes but the model remains a planner.
    runtime = ApiRuntime(build_demo_platform(mode="flow"))
    handle, mode = runtime.start_run(
        "Navigate to example.org and check the model list", mode="react"
    )
    try:
        state = await handle.task
        assert mode == "react"
        assert state.status is RunStatus.COMPLETED
        assert isinstance(json.loads(state.final_answer), list)
        assert not state.results
        assert state.snapshot()["execution"]["goal_verification"] == "not_verified"
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "mode,task,expected_mode,executor",
    [
        ("react", "Navigate to example.org and check the model list", "react", "demo-react"),
        ("flow", "Execute the plan", "flow", "demo-planner"),
        ("auto", "Plan a multi-step research comparison", "flow", "demo-planner"),
        ("auto", "Sum 0..100", "react", "demo-react"),
    ],
)
async def test_fixed_demo_factory_binds_executor_before_run(mode, task, expected_mode, executor):
    runtime = ApiRuntime(lambda m, n: build_demo_platform_for(m, max_steps=n))
    try:
        handle, chosen_mode = runtime.start_run(task, mode=mode, max_steps=9)
        assert chosen_mode == expected_mode
        assert handle.platform.model.name == executor
        assert runtime.get_run_snapshot(handle.run_id)["execution"]["model_name"] == executor
        state = await handle.task
        assert state.status is RunStatus.COMPLETED
        assert state.mode == expected_mode
        assert state.results, "the transcript must invoke tools, not return a raw DAG as its answer"
        assert not state.final_answer.lstrip().startswith("[{")
        snap = runtime.get_run_snapshot(handle.run_id)
        assert snap["execution"] == {
            "kind": "scripted",
            "model_name": executor,
            "goal_verification": "not_verified",
        }
        assert snap["recorded_tool_results"] == len(state.results)
        assert handle.platform.config.max_steps == 9
    finally:
        runtime.close()


def test_execution_descriptor_is_allowlisted_and_never_claims_model_identity():
    assert describe_execution(ScriptedModel(name="demo")).kind == "scripted"
    model = OpenAICompatModel(
        base_url="https://invalid.example/v1", model_id="configured-model", api_key="DO-NOT-EXPOSE"
    )
    description = describe_execution(model).model_dump()
    assert description["kind"] == "provider_adapter"
    assert description["goal_verification"] == "not_verified"
    serialized = json.dumps(description)
    assert "DO-NOT-EXPOSE" not in serialized
    assert "invalid.example" not in serialized
    assert "configured-model" not in serialized
    assert describe_execution(object()).kind == "unknown"


def test_old_snapshot_remains_unknown_not_inferred_from_router():
    state = RunState.model_validate({"run_id": "old", "status": "completed"})
    snap = state.snapshot()
    assert snap["status"] == "completed"
    assert snap["execution"]["kind"] == "unknown"
    assert snap["execution"]["goal_verification"] == "not_verified"


def test_runtime_info_requires_platform_auth_and_does_not_pollute_health():
    app = create_app(lambda m, n: build_demo_platform_for(m, max_steps=n), api_token="test")
    with TestClient(app) as client:
        assert client.get("/runtime-info").status_code == 401
        assert client.get("/health").json() == {"status": "ok"}
        info = client.get("/runtime-info", headers={"Authorization": "Bearer test"}).json()
        assert info == {
            "execution": {
                "kind": "scripted",
                "model_name": "demo-react",
                "goal_verification": "not_verified",
            }
        }
    app.state.runtime.close()


async def test_executor_info_survives_temporary_sqlite_restart(tmp_path):
    url = f"sqlite:///{tmp_path / 'disclosure.db'}"

    def factory(m, n):
        return build_demo_platform_for(m, max_steps=n)

    first = ApiRuntime(factory, store_url=url)
    handle, _ = first.start_run("Sum 0..100", mode="react")
    state = await handle.task
    expected = first.get_run_snapshot(state.run_id)
    first.close()
    second = ApiRuntime(factory, store_url=url)
    try:
        snapshot = second.get_run_snapshot(state.run_id)
        assert snapshot["execution"] == expected["execution"]
        assert snapshot["recorded_tool_results"] == expected["recorded_tool_results"]
    finally:
        second.close()


@pytest.mark.parametrize(
    "observation", ["ERROR: isolated executor unavailable", "DENIED by policy", "", "123"]
)
async def test_demo_does_not_claim_calculation_when_tool_did_not_produce_it(observation):
    model = demo_react_model("task")
    response = await model.complete([Message(role="tool", content=observation)])
    assert "not verified" in response.content
    assert "5050" not in response.content
