"""Shared fixtures for the platform test-suite."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import pytest
from os_core.events import EventBus
from os_core.ledger import LEDGER_ENV_VAR
from os_core.models import ModelResponse, Usage
from os_core.router import ModelRouter, RouterConfig
from os_core.types import ToolCall
from os_tools.browser import BrowserTool, FakeBrowser, PageSnapshot, RetryPolicy
from os_tools.code_exec import LocalSubprocessBackend, python_execute_tool
from os_tools.registry import ToolRegistry
from os_tools.ssrf import SSRFGuard

T = TypeVar("T")

#: One ceiling for every bounded wait in the suite. Generous on purpose: a wait
#: is a *ceiling*, never the thing under test — a slow CI runner may take longer
#: than a laptop, and the assertion must not be "the machine was fast enough".
WAIT_CEILING = 10.0


def wait_until(
    probe: Callable[[], T],
    *,
    what: str,
    ceiling: float = WAIT_CEILING,
    interval: float = 0.02,
) -> T:
    """Poll ``probe`` until it returns a truthy value; fail with the ceiling named.

    For synchronous surfaces (``TestClient``). Prefer an event-driven wait when
    the component offers one (``QueueApprover.wait_for_request``, futures,
    ``server.started`` …); use this only where the only signal is a snapshot.
    """
    deadline = time.monotonic() + ceiling
    value = probe()
    while not value:
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{what}: not reached within the {ceiling:.0f}s ceiling ({value!r})"
            )
        time.sleep(interval)
        value = probe()
    return value


async def await_until(
    probe: Callable[[], T | Awaitable[T]],
    *,
    what: str,
    ceiling: float = WAIT_CEILING,
    interval: float = 0.01,
) -> T:
    """Async twin of :func:`wait_until`; ``probe`` may be sync or async."""
    deadline = time.monotonic() + ceiling
    while True:
        value = probe()
        if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
            value = await value  # type: ignore[assignment]
        if value:
            return value  # type: ignore[return-value]
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{what}: not reached within the {ceiling:.0f}s ceiling ({value!r})"
            )
        await asyncio.sleep(interval)


@pytest.fixture(autouse=True)
def ledger_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite hermetic: no test writes the default ledger file.

    The application surfaces (CLI, server) record every execution by default
    into ``./.cela/ledger.jsonl``. Tests that exercise the ledger configure one
    explicitly under ``tmp_path``; a test that needs the default resolution
    deletes this variable itself.
    """
    monkeypatch.setenv(LEDGER_ENV_VAR, "off")


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


@pytest.fixture
def browser_registry(registry: ToolRegistry, browser_tool: BrowserTool) -> ToolRegistry:
    """Registry pre-loaded with ``browser_tools`` for agent-level tests."""
    from os_tools.browser import browser_tools
    from os_tools.ssrf import SSRFGuard

    for t in browser_tools(browser_tool, ssrf_check=SSRFGuard(resolve_dns=False).check):
        registry.add(t)
    return registry


def _make_tool_response(*calls: ToolCall, content: str = "") -> ModelResponse:
    return ModelResponse(
        content=content, tool_calls=list(calls), usage=Usage(prompt_tokens=50, completion_tokens=20)
    )


@pytest.fixture
def tool_response():
    """Factory for a model response that requests tool calls.

    Deliberately a fixture, not an importable helper: test modules must never
    write ``from tests.conftest import ...``, because that depends on the
    repository root being on ``sys.path`` — which pytest 9.1 no longer
    guarantees. Injecting it keeps collection working on any pytest version.
    """
    return _make_tool_response
