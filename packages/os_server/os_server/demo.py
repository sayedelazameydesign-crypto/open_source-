"""Offline demo stack — runs the whole platform with zero API keys.

``build_demo_platform()`` wires:

* a scripted planner/executor model (stands in for DeepSeek-R1 / Qwen),
* the real sandboxed code interpreter (subprocess + rlimits),
* the fake browser (real retry/vision-fallback logic, no Chromium needed),
* the SSRF guard, guardrails and a HITL queue.

Swap :class:`ScriptedModel` for :class:`OpenAICompatModel` pointed at vLLM or
Ollama and the exact same code path drives real models.
"""

from __future__ import annotations

import json
from typing import Any

from os_core.hitl import QueueApprover
from os_core.models import ModelResponse, ScriptedModel, Usage
from os_core.router import ModelRouter, RouterConfig
from os_core.types import ToolCall
from os_orchestration.platform import Platform, PlatformConfig
from os_tools.browser import BrowserTool, FakeBrowser, PageSnapshot, RetryPolicy
from os_tools.code_exec import LocalSubprocessBackend
from os_tools.registry import ToolRegistry
from os_tools.ssrf import SSRFGuard


def demo_browser() -> BrowserTool:
    """A fake site whose submit button mutates out from under the selector."""
    home = PageSnapshot(
        url="https://example.org/search",
        title="Example Search",
        html="<form><input id='q'><button id='go'>Search</button></form>",
        elements=[
            {"index": 0, "tag": "input", "id": "q", "text": ""},
            {"index": 1, "tag": "button", "id": "go", "text": "Search"},
        ],
        screenshot_b64="iVBORw0KGgo=",
    )
    results = PageSnapshot(
        url="https://example.org/results",
        title="Results",
        html="<ul><li>DeepSeek-R1</li><li>Qwen2.5</li></ul>",
        elements=[{"index": 0, "tag": "li", "text": "DeepSeek-R1"}],
        screenshot_b64="iVBORw0KGgo=",
    )
    backend = FakeBrowser(
        pages={
            "https://example.org/search": home,
            "https://example.org/results": results,
        },
        # '#go' stops resolving after the first attempt -> exercises the fallback.
        fail_until_attempt={"#go": 1},
    )
    return BrowserTool(backend=backend, policy=RetryPolicy(max_attempts=2, backoff=0.0))


def build_registry() -> tuple[ToolRegistry, SSRFGuard, BrowserTool]:
    from os_tools.browser import browser_tools
    from os_tools.code_exec import python_execute_tool

    registry = ToolRegistry(default_timeout=30.0)
    guard = SSRFGuard()
    browser = demo_browser()
    registry.add(python_execute_tool(LocalSubprocessBackend(), timeout=20.0))
    registry.extend(browser_tools(browser, ssrf_check=guard.check))
    return registry, guard, browser


def demo_react_model(task: str) -> ScriptedModel:
    """Deterministic ReAct transcript: one code step, then a final answer."""

    def responder(messages: Any) -> ModelResponse:
        seen_tools = [m for m in messages if m.role == "tool"]
        if not seen_tools:
            return ModelResponse(
                content="I will compute the answer with the sandboxed interpreter.",
                tool_calls=[
                    ToolCall(
                        name="python_execute",
                        args={
                            "code": "import sys\nprint(sum(range(101)))\nprint(sys.version_info[0])"
                        },
                    )
                ],
                usage=Usage(prompt_tokens=120, completion_tokens=30),
            )
        observation = seen_tools[-1].content
        return ModelResponse(
            content=f"The sandbox returned: {observation.splitlines()[0].strip()}. "
            "The sum of 0..100 is 5050.",
            usage=Usage(prompt_tokens=180, completion_tokens=40),
        )

    return ScriptedModel(name="demo-react", responder=responder)


def demo_planner_model(task: str) -> ScriptedModel:
    """Deterministic DAG plan + synthesis, for the flow mode."""
    plan = json.dumps(
        [
            {
                "id": "s1",
                "title": "Compute the dataset size",
                "tool": "python_execute",
                "args": {"code": "print(len([x for x in range(1000) if x % 7 == 0]))"},
                "depends_on": [],
            },
            {
                "id": "s2",
                "title": "Check the open-source model list",
                "tool": "browser_navigate",
                "args": {"url": "https://example.org/search"},
                "depends_on": [],
            },
            {
                "id": "s3",
                "title": "Combine findings",
                "tool": "python_execute",
                "args": {"code": "print('multiples of 7 below 1000:', 143)"},
                "depends_on": ["s1", "s2"],
            },
        ]
    )
    return ScriptedModel(
        name="demo-planner",
        script=[ModelResponse(content=plan, usage=Usage(prompt_tokens=400, completion_tokens=150))],
        final_content=(
            "Plan complete. The sandbox counted 143 multiples of 7 below 1000 and the "
            "browser confirmed the model list page loaded."
        ),
    )


def build_demo_platform_for(mode: str, *, interactive: bool = False) -> Platform:
    """A platform whose scripted model matches the requested execution mode.

    Each run gets its own instance: the run must never mutate shared config,
    otherwise a later request inherits an earlier one's mode.
    """
    return build_demo_platform(mode="flow" if mode == "flow" else "react", interactive=interactive)


def build_demo_platform(*, mode: str = "auto", interactive: bool = False) -> Platform:
    registry, _guard, _browser = build_registry()
    model = demo_planner_model("") if mode == "flow" else demo_react_model("")
    return Platform(
        model=model,
        registry=registry,
        config=PlatformConfig(mode=mode, max_steps=8, token_budget=50_000),
        router=ModelRouter(config=RouterConfig(token_budget=50_000)),
        approver=QueueApprover(timeout=60.0) if interactive else QueueApprover(timeout=1.0),
    )
