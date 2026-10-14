"""Production isolation backends: fail-closed, never a silent local fallback."""

from __future__ import annotations

import pytest
from os_core.types import ToolStatus
from os_tools.code_exec import (
    E2BBackend,
    FirecrackerBackend,
    LocalSubprocessBackend,
    code_exec_backend_from_env,
)


def _factory_raises():
    raise RuntimeError("no sandbox available")


class _FakeExecution:
    logs = type("L", (), {"stdout": ["5050\n"]})()
    error = None
    text = None


class _FakeSandbox:
    killed = False

    def run_code(self, code: str):
        assert "sum" in code
        return _FakeExecution()

    def kill(self) -> None:
        _FakeSandbox.killed = True


async def test_e2b_missing_infrastructure_denies_without_executing() -> None:
    result = await E2BBackend(sandbox_factory=_factory_raises).execute(
        "print(1)", language="python", files=None, timeout=5
    )
    assert result.status is ToolStatus.DENIED
    assert result.metadata["executed"] is False
    assert "fail-closed" in (result.error or "")


async def test_e2b_fake_sandbox_executes_and_is_killed() -> None:
    result = await E2BBackend(sandbox_factory=_FakeSandbox).execute(
        "print(sum(range(101)))", language="python", files=None, timeout=5
    )
    assert result.status is ToolStatus.SUCCESS
    assert "5050" in result.output
    assert result.metadata["isolation"] == "e2b-microvm"
    assert _FakeSandbox.killed is True


async def test_firecracker_without_endpoint_denies() -> None:
    result = await FirecrackerBackend(endpoint=None, endpoint_env="CELA_FC_MISSING_XYZ").execute(
        "print(1)", language="python", files=None, timeout=5
    )
    assert result.status is ToolStatus.DENIED
    assert result.metadata["executed"] is False


async def test_firecracker_manager_roundtrip_and_failure_are_not_local() -> None:
    httpx = pytest.importorskip("httpx")

    def handler(request):
        assert request.url.path == "/execute"
        return httpx.Response(200, json={"output": "ok", "status": "success", "files": []})

    backend = FirecrackerBackend(
        endpoint="http://manager.local", transport=httpx.MockTransport(handler)
    )
    result = await backend.execute("print('ok')", language="python", files=None, timeout=5)
    assert result.status is ToolStatus.SUCCESS and result.output == "ok"
    assert result.metadata["isolation"] == "firecracker-microvm"

    def down(request):
        return httpx.Response(503, text="boot failed")

    failing = FirecrackerBackend(
        endpoint="http://manager.local", transport=httpx.MockTransport(down)
    )
    denied = await failing.execute("print(1)", language="python", files=None, timeout=5)
    assert denied.status is ToolStatus.DENIED and denied.metadata["executed"] is False


def test_env_selection_defaults_local_and_production_never_does() -> None:
    assert isinstance(code_exec_backend_from_env({}), LocalSubprocessBackend)
    assert isinstance(code_exec_backend_from_env({"CELA_CODE_EXEC_MODE": "e2b"}), E2BBackend)
    assert isinstance(
        code_exec_backend_from_env(
            {"CELA_CODE_EXEC_MODE": "production", "CELA_CODE_EXEC_BACKEND": "firecracker"}
        ),
        FirecrackerBackend,
    )
    with pytest.raises(RuntimeError, match="refusing to fall back"):
        code_exec_backend_from_env({"CELA_CODE_EXEC_MODE": "production"})
    with pytest.raises(RuntimeError, match="unknown"):
        code_exec_backend_from_env({"CELA_CODE_EXEC_MODE": "docker"})
