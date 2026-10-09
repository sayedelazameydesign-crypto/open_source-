"""Shared fixtures for the platform test-suite."""

from __future__ import annotations

import pytest
from os_core.events import EventBus
from os_core.models import ModelResponse, ScriptedModel, Usage
from os_core.router import ModelRouter, RouterConfig
from os_core.types import ToolCall
from os_tools.browser import BrowserTool, FakeBrowser, PageSnapshot, RetryPolicy
from os_tools.code_exec import LocalSubprocessBackend, python_execute_tool
from os_tools.registry import ToolRegistry
from os_tools.ssrf import SSRFGuard


@pytest.fixture
def bus() -> EventBus:
    return EventBus(run_id="run_test")


@pytest.fixture
def events(bus: EventBus) -> list:
    """Collect every published event so tests can assert on the stream."""
    collected: list = []
    bus.subscribe(collected.append)
    return collected


@pytest.fixture
def router() -> ModelRouter:
    return ModelRouter(config=RouterConfig(token_budget=10_000))


@pytest.fixture
def guard() -> SSRFGuard:
    # DNS disabled: tests must be hermetic and must not hit the network.
    return SSRFGuard(resolve_dns=False)


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry(default_timeout=20.0)
    reg.add(python_execute_tool(LocalSubprocessBackend(), timeout=10.0))

    @reg.register(
        "echo",
        "Return the text unchanged.",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )
    def echo(text: str) -> str:
        return text

    @reg.register(
        "flaky",
        "Fails the first two calls, then succeeds — exercises retry.",
        {"type": "object", "properties": {}},
        retries=2,
    )
    async def flaky() -> str:
        state = getattr(flaky, "calls", 0) + 1
        flaky.calls = state  # type: ignore[attr-defined]
        if state < 3:
            raise RuntimeError(f"transient failure #{state}")
        return "recovered"

    flaky.calls = 0  # type: ignore[attr-defined]
    return reg


@pytest.fixture
def fake_browser() -> FakeBrowser:
    home = PageSnapshot(
        url="https://example.org/search",
        title="Search",
        elements=[{"index": 0, "tag": "button", "id": "go", "text": "Search"}],
        screenshot_b64="iVBORw0KGgo=",
    )
    return FakeBrowser(pages={"https://example.org/search": home})


@pytest.fixture
def browser_tool(fake_browser: FakeBrowser) -> BrowserTool:
    return BrowserTool(backend=fake_browser, policy=RetryPolicy(max_attempts=2, backoff=0.0))


def scripted(*responses: ModelResponse, final: str = "done") -> ScriptedModel:
    return ScriptedModel(script=list(responses), final_content=final)


def tool_response(*calls: ToolCall, content: str = "") -> ModelResponse:
    return ModelResponse(
        content=content, tool_calls=list(calls), usage=Usage(prompt_tokens=50, completion_tokens=20)
    )


def text_response(content: str, *, tokens: int = 40) -> ModelResponse:
    return ModelResponse(content=content, usage=Usage(prompt_tokens=50, completion_tokens=tokens))
