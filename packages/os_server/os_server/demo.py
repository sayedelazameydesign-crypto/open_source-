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
import os
from typing import Any

from os_core.hitl import QueueApprover
from os_core.models import ModelResponse, ScriptedModel, Usage
from os_core.router import ModelRouter, RouterConfig
from os_core.types import ToolCall
from os_orchestration.platform import Platform, PlatformConfig
from os_tools.browser import BrowserTool, FakeBrowser, PageSnapshot, RetryPolicy
from os_tools.code_exec import code_exec_backend_from_env
from os_tools.egress import EgressPolicy
from os_tools.registry import ToolRegistry
from os_tools.ssrf import SSRFGuard


def demo_egress_policy() -> EgressPolicy:
    """Default-deny egress policy for the offline demo: only example.org is allowed."""
    return EgressPolicy.allow_hosts(
        "example.org",
        "*.example.org",
        schemes={"https"},
        ports={443},
        tools={"browser"},
        resolve_dns=False,
    )


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


def build_registry(
    *,
    egress_policy: EgressPolicy | None = None,
    ai_engine: Any | None = None,
) -> tuple[ToolRegistry, SSRFGuard, BrowserTool]:
    from os_tools.browser import browser_tools
    from os_tools.code_exec import python_execute_tool
    from os_tools.local_ai import LocalAIEngine, local_ai_tools

    registry = ToolRegistry(default_timeout=30.0)
    policy = egress_policy if egress_policy is not None else demo_egress_policy()
    guard = policy.guard
    browser = demo_browser()
    engine = ai_engine if ai_engine is not None else LocalAIEngine()
    registry.add(python_execute_tool(code_exec_backend_from_env(), timeout=20.0))
    registry.extend(browser_tools(browser, ssrf_check=policy.for_tool("browser")))
    registry.extend(local_ai_tools(engine))
    if os.environ.get("CELA_REACH_ENABLE") == "1":
        # Reach tools (ADR-0013) are opt-in: they fetch the public internet, so
        # enabling them is a deployment decision rather than a default. They
        # carry their own public-internet policy (narrowable with
        # CELA_REACH_ALLOW_HOSTS / CELA_REACH_DENY_HOSTS) on top of the SSRF
        # floor, so this does not widen the browser's egress policy.
        from os_reach.adapters.cli import build_reach_layer
        from os_reach.tools import register_reach_tools

        # The native tier keeps its own public-internet policy (narrowable with
        # CELA_REACH_ALLOW_HOSTS / CELA_REACH_DENY_HOSTS) — that is what makes
        # enabling reach *not* a widening of the browser policy above. The CLI
        # switches (CELA_REACH_CLI_ENABLE, CELA_REACH_CLI_ALLOW_AUTH) are read by
        # build_reach_layer: with neither set the CLI half stays inert and this is
        # the native-only wiring as before.
        register_reach_tools(registry, reach=build_reach_layer())
    # Memory and corpus tools are opt-in the same way the skill store is: the
    # store URL's presence *is* the switch. Nothing sets it in CI, so the demo and
    # every existing test stay hermetic — and no deployment gets a durable memory
    # layer it did not ask for.
    memory = demo_memory_manager(engine)
    if memory is not None:
        from os_memory import memory_tools

        registry.extend(memory_tools(memory))
    know = demo_know_layer(engine)
    if know is not None:
        from os_know import know_tools

        registry.extend(know_tools(*know))
    # MCP tools (ADR-0017) join the registry only after the process-wide layer
    # has *discovered* its servers — an async step the server performs in its
    # lifespan hook and the CLI performs before a run. Before discovery there
    # is nothing to add, and with CELA_MCP_ENABLE unset there is no layer.
    from os_mcp.layer import process_layer, register_mcp_tools

    mcp = process_layer()
    if mcp is not None and mcp.discovered:
        register_mcp_tools(registry, mcp)
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
        first_line = (observation.splitlines() or ["(empty output)"])[0].strip()
        answer = (
            "The sandbox returned: 5050. The sum of 0..100 is 5050."
            if first_line == "5050"
            else f"Demo calculation not verified. Tool observation: {first_line}"
        )
        return ModelResponse(
            content=answer,
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
            "Demo transcript finished. Inspect the recorded tool results and their statuses. "
            "This scripted response does not verify the user's goal or real browser access."
        ),
    )


def build_demo_platform_for(
    mode: str, *, interactive: bool = False, max_steps: int = 8
) -> Platform:
    """A platform whose scripted model matches the requested execution mode.

    Each run gets its own instance: the run must never mutate shared config,
    otherwise a later request inherits an earlier one's mode.
    """
    platform = build_demo_platform(mode=mode, interactive=interactive)
    platform.config.max_steps = max_steps
    return platform


_SKILL_MANAGER = None


_MEMORY_MANAGER: Any = None
_KNOW_LAYER: Any = None


ENV_MEMORY_NAMESPACE = "CELA_MEMORY_NAMESPACE"


def demo_memory_manager(ai_engine: Any = None) -> Any:
    """Process-wide memory manager when ``CELA_MEMORY_STORE_URL`` is set (ADR-0016).

    Shared for the same reason as the skill manager: the server builds a platform
    per run, and a manager per platform could never accumulate anything. Every
    write still passes :class:`os_memory.MemoryGate`.

    The manager is bound to **one** namespace, ``CELA_MEMORY_NAMESPACE``
    (default ``default``). That is the honest granularity for this API: it
    authenticates callers with a single shared bearer token, so every caller is
    the same principal and there is no identity a per-request namespace could be
    derived from. When the API grows principals, ``manager.scoped(ns)`` is the
    one-line binding point; until then the operator names the space.
    """
    global _MEMORY_MANAGER
    url = os.environ.get("CELA_MEMORY_STORE_URL")
    if not url:
        return None
    if _MEMORY_MANAGER is None:
        from os_tools.local_ai import LocalAIEngine

        from os_memory import MemoryManager, memory_store_from_url, normalize_namespace

        engine = ai_engine if ai_engine is not None else LocalAIEngine()
        _MEMORY_MANAGER = MemoryManager(
            memory_store_from_url(url),
            engine,
            namespace=normalize_namespace(os.environ.get(ENV_MEMORY_NAMESPACE)),
        )
    return _MEMORY_MANAGER


def demo_know_layer(ai_engine: Any = None) -> tuple[Any, Any] | None:
    """``(store, retriever)`` for ``CELA_KNOW_STORE_URL``; ``None`` when unset."""
    global _KNOW_LAYER
    url = os.environ.get("CELA_KNOW_STORE_URL")
    if not url:
        return None
    if _KNOW_LAYER is None:
        from os_tools.local_ai import LocalAIEngine

        from os_know import KnowRetriever, know_store_from_url

        engine = ai_engine if ai_engine is not None else LocalAIEngine()
        store = know_store_from_url(url)
        _KNOW_LAYER = (store, KnowRetriever(store, engine))
    return _KNOW_LAYER


def demo_skill_manager():
    """One process-wide skill manager when ``CELA_SKILL_STORE_URL`` is set.

    Shared deliberately: the server builds a platform per run, so a manager
    per platform could never accumulate a library. Unset env = feature off,
    which keeps the offline demo and every existing test hermetic.
    """
    global _SKILL_MANAGER
    url = os.environ.get("CELA_SKILL_STORE_URL")
    if not url:
        return None
    if _SKILL_MANAGER is None:
        from os_tools.local_ai import LocalAIEngine

        from os_skills import SkillManager, skill_store_from_url

        _SKILL_MANAGER = SkillManager(skill_store_from_url(url), LocalAIEngine())
    return _SKILL_MANAGER


def build_demo_platform(*, mode: str = "auto", interactive: bool = False) -> Platform:
    registry, _guard, _browser = build_registry()
    model = demo_planner_model("") if mode == "flow" else demo_react_model("")
    return Platform(
        model=model,
        registry=registry,
        config=PlatformConfig(mode=mode, max_steps=8, token_budget=50_000),
        router=ModelRouter(config=RouterConfig(token_budget=50_000)),
        approver=QueueApprover(timeout=60.0) if interactive else QueueApprover(timeout=1.0),
        skill_manager=demo_skill_manager(),
        memory_manager=demo_memory_manager(),
    )
