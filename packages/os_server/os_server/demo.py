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
from collections.abc import Sequence
from typing import Any

from os_core.hitl import QueueApprover
from os_core.models import ChatModel, ModelResponse, OpenAICompatModel, ScriptedModel, Usage
from os_core.redact import scrub_credentials
from os_core.router import ModelRouter, RouterConfig
from os_core.types import Message, ToolCall
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
    from os_tools.mission import mission_tools

    registry = ToolRegistry(default_timeout=30.0)
    policy = egress_policy if egress_policy is not None else demo_egress_policy()
    guard = policy.guard
    browser = demo_browser()
    engine = ai_engine if ai_engine is not None else LocalAIEngine()
    registry.add(python_execute_tool(code_exec_backend_from_env(), timeout=20.0))
    registry.extend(browser_tools(browser, ssrf_check=policy.for_tool("browser")))
    registry.extend(local_ai_tools(engine))
    registry.extend(mission_tools(root=os.environ.get("CELA_MISSION_ROOT", ".")))
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
    # Sub-agents (ADR-0020) are opt-in the same way: the config directory's
    # presence *is* the switch. Point CELA_SUBAGENTS_DIR at agents/sub_agents
    # and the orchestrator gains delegate_subagent/subagent_list over local
    # read-only workers; unset, no roster exists and nothing changes.
    subagents_dir = os.environ.get("CELA_SUBAGENTS_DIR")
    if subagents_dir:
        from os_agents.subagents import hub_from_directory, subagent_tools

        hub = hub_from_directory(subagents_dir, root=os.environ.get("CELA_SUBAGENTS_ROOT", "."))
        registry.extend(subagent_tools(hub))
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


def _extract_user_task(messages: Sequence[Message], fallback: str = "") -> str:
    for msg in messages:
        if msg.role == "user" and "Task:" in msg.content:
            return msg.content.split("Task:", 1)[1].strip()
    for msg in messages:
        if msg.role == "user":
            return msg.content.strip()
    return fallback


def _is_mission_task(text: str) -> bool:
    lowered = text.lower()
    return any(
        kw in lowered
        for kw in (
            "افحص",
            "خطة نشر",
            "تقرير مهمة",
            "mission os",
        )
    )


def demo_planner_model(task: str) -> ScriptedModel:
    """Deterministic DAG plan + synthesis, for the flow mode."""
    default_plan = json.dumps(
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
    default_final = (
        "Demo transcript finished. Inspect the recorded tool results and their statuses. "
        "This scripted response does not verify the user's goal or real browser access."
    )
    planned = False
    mission_mode = False

    def responder(messages: Sequence[Message]) -> ModelResponse:
        nonlocal planned, mission_mode
        extracted_task = _extract_user_task(messages, task)
        if not planned:
            planned = True
            mission_mode = _is_mission_task(task or extracted_task)
            if mission_mode:
                first_user = extracted_task or "افحص مشروعي وأنشئ خطة نشر مجانية وآمنة"
                mission_steps = [
                    {
                        "id": "s1",
                        "title": "فحص بنية المشروع وملفات الإعداد فحصًا ساكنًا",
                        "tool": "inspect_project_files",
                        "args": {"path": "."},
                        "depends_on": [],
                    },
                    {
                        "id": "s2",
                        "title": "فحص حزمة الإعدادات ومسارات التكامل المستمر",
                        "tool": "inspect_project_files",
                        "args": {"path": ".", "max_files": 50},
                        "depends_on": ["s1"],
                    },
                    {
                        "id": "s3",
                        "title": "توليد تقرير المهمة وخطة النشر المجانية (MD / JSON / HTML)",
                        "tool": "generate_deployment_plan_report",
                        "args": {
                            "project_name": "Celia.sh Workspace",
                            "goal": first_user[:240],
                            "inspection_summary": "{{s1.raw}}",
                        },
                        "depends_on": ["s1", "s2"],
                    },
                ]
                return ModelResponse(
                    content=json.dumps(mission_steps, ensure_ascii=False),
                    usage=Usage(prompt_tokens=260, completion_tokens=140),
                )
            return ModelResponse(
                content=default_plan,
                usage=Usage(prompt_tokens=400, completion_tokens=150),
            )
        if mission_mode:
            return ModelResponse(
                content=(
                    "اكتمل الفحص الساكن وتوليد التقارير الثلاثة (mission-report.md / "
                    "mission-report.json / mission-report.html) ببصمة SHA-256. راجع "
                    "الأدلة المسجلة وتنزيل الملفات؛ لم يتم ادعاء تشغيل اختبارات لم تُنفَّذ."
                ),
                usage=Usage(prompt_tokens=300, completion_tokens=90),
            )
        return ModelResponse(
            content=default_final,
            usage=Usage(prompt_tokens=300, completion_tokens=80),
        )

    return ScriptedModel(
        name="demo-planner",
        script=[
            ModelResponse(
                content=default_plan,
                usage=Usage(prompt_tokens=400, completion_tokens=150),
            )
        ],
        final_content=default_final,
        responder=responder,
    )


def build_demo_platform_for(
    mode: str,
    *,
    interactive: bool = False,
    max_steps: int = 8,
    use_env_provider: bool | None = None,
    transport: Any = None,
    require_live: bool = False,
) -> Platform:
    """A platform whose model matches the requested execution mode.

    When ``CELA_USE_LIVE_PROVIDER=1`` (or ``use_env_provider=True``) a real
    OpenAI-compatible endpoint (Gemini / Groq / OpenRouter / explicit
    ``CELA_PROVIDER_*``) is wrapped in :class:`ResilientModel` so a quota /
    network failure degrades honestly to the scripted fallback — and
    ``describe_execution`` reports ``scripted`` rather than pretending the
    provider answered.
    """
    if use_env_provider is None:
        use_env_provider = is_live_provider_enabled()
    platform = build_demo_platform(
        mode=mode,
        interactive=interactive,
        use_env_provider=use_env_provider,
        transport=transport,
        require_live=require_live,
    )
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


def build_demo_platform(
    *,
    mode: str = "auto",
    interactive: bool = False,
    use_env_provider: bool = False,
    primary_model: ChatModel | None = None,
    transport: Any = None,
    require_live: bool = False,
) -> Platform:
    """Build a demo/live platform.

    When live provider mode is enabled (CELA_USE_LIVE_PROVIDER=1 or
    use_env_provider=True) a valid provider is resolved via
    :func:`resolve_platform_model` and wrapped with an honest scripted
    fallback. If ``require_live=True`` and no provider is available, the
    function raises RuntimeError rather than silently serving scripted runs
    — this is what prevents a Render deploy from advertising
    ``provider_adapter`` while actually running offline.
    """
    registry, _guard, _browser = build_registry()
    if primary_model is not None:
        # Caller supplied an explicit model (tests / forced failure paths).
        from os_core.models import describe_execution as _describe

        if isinstance(primary_model, ResilientModel):
            model: ChatModel = primary_model
        else:
            fallback: ScriptedModel = (
                demo_planner_model("") if mode == "flow" else demo_react_model("")
            )
            model = ResilientModel(
                fallback=fallback, primary=primary_model, label="explicit"
            )
        mode_info = {"mode": "explicit", "kind": _describe(model).kind}
    else:
        model, mode_info = resolve_platform_model(
            mode=mode,
            transport=transport,
            require_live=require_live and use_env_provider,
        )
    return Platform(
        model=model,
        registry=registry,
        config=PlatformConfig(mode=mode, max_steps=8, token_budget=50_000),
        router=ModelRouter(config=RouterConfig(token_budget=50_000)),
        approver=QueueApprover(timeout=60.0) if interactive else QueueApprover(timeout=1.0),
        skill_manager=demo_skill_manager(),
        memory_manager=demo_memory_manager(),
    )


def demo_mission_planner_model(
    *,
    project_name: str = "Celia.sh",
    goal: str = "افحص مشروعي، واكتشف نقاط الضعف، ثم أنشئ خطة نشر مجانية وآمنة",
    path: str = ".",
    url: str | None = None,
) -> ScriptedModel:
    """Deterministic 3-step Mission OS DAG plan (offline, zero API keys)."""
    steps: list[dict[str, Any]] = [
        {
            "id": "s1",
            "title": "فحص بنية المشروع وملفات الإعداد فحصًا ساكنًا",
            "tool": "inspect_project_files",
            "args": {"path": path},
            "depends_on": [],
        }
    ]
    report_deps = ["s1"]
    report_args: dict[str, Any] = {
        "project_name": project_name,
        "goal": goal,
        "inspection_summary": "{{s1.raw}}",
    }
    if url:
        steps.append(
            {
                "id": "s2",
                "title": "فحص الرابط العام وترويسات الأمان",
                "tool": "inspect_public_url",
                "args": {"url": url},
                "depends_on": [],
            }
        )
        report_deps.append("s2")
        report_args["url_check_summary"] = "{{s2.raw}}"
    steps.append(
        {
            "id": "s3",
            "title": "توليد تقرير المهمة وخطة النشر المجانية (MD / JSON / HTML)",
            "tool": "generate_deployment_plan_report",
            "args": report_args,
            "depends_on": report_deps,
        }
    )
    return ScriptedModel(
        name="demo-mission-planner",
        script=[
            ModelResponse(
                content=json.dumps(steps, ensure_ascii=False),
                usage=Usage(prompt_tokens=260, completion_tokens=140),
            )
        ],
        final_content=(
            "اكتمل الفحص الساكن وتوليد التقارير الثلاثة (MD / JSON / HTML) ببصمة SHA-256. "
            "راجع الأدلة المسجلة؛ لم يتم ادعاء تشغيل اختبارات لم تُنفَّذ."
        ),
    )


def is_live_provider_enabled() -> bool:
    """Return True when the operator has explicitly opted into live LLM calls.

    Gated by CELA_USE_LIVE_PROVIDER=1/true/yes/on so a default Render deploy
    (where the env var is unset) never makes outbound LLM calls without
    operator consent.
    """
    return os.environ.get("CELA_USE_LIVE_PROVIDER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _provider_from_env_explicit(
    *, transport: Any = None, timeout: float = 60.0
) -> OpenAICompatModel | None:
    """Build a provider from explicit CELA_PROVIDER_* env vars (highest priority).

    Recognised variables (matches the operator command in the deployment plan):
        CELA_PROVIDER_CREDENTIAL_VAR  name of the env var holding the credential
                                      (default GEMINI_API_KEY).
        CELA_PROVIDER_API_KEY_ENV     legacy alias for CELA_PROVIDER_CREDENTIAL_VAR,
                                      kept for backwards compatibility with
                                      earlier runbooks and local shells.
        CELA_PROVIDER_BASE_URL        OpenAI-compatible base URL ending in /v1
                                      or /v1beta/openai.  Legacy aliases:
                                      CELA_MODEL_BASE_URL.
        CELA_PROVIDER_MODEL           model id to request.  Legacy alias:
                                      CELA_MODEL_ID.
    """
    # Resolution order for base_url:
    #   1. CELA_PROVIDER_BASE_URL (current canonical name)
    #   2. CELA_MODEL_BASE_URL    (legacy GitHub-secrets name, for existing
    #      deployments that still set it)
    base_url = (
        os.environ.get("CELA_PROVIDER_BASE_URL", "").strip()
        or os.environ.get("CELA_MODEL_BASE_URL", "").strip()
    )
    model_id = (
        os.environ.get("CELA_PROVIDER_MODEL", "").strip()
        or os.environ.get("CELA_MODEL_ID", "").strip()
    )
    # Backwards-compat: accept the original CELA_PROVIDER_API_KEY_ENV if set,
    # preferring CELA_PROVIDER_CREDENTIAL_VAR when both are present.
    api_key_env = (
        os.environ.get("CELA_PROVIDER_CREDENTIAL_VAR", "").strip()
        or os.environ.get("CELA_PROVIDER_API_KEY_ENV", "").strip()
        or "GEMINI_API_KEY"
    )
    if not base_url or not model_id:
        return None
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        return None
    # Derive a human-readable name from the env var / model id.
    label = api_key_env.replace("_API_KEY", "").lower()
    return OpenAICompatModel(
        name=f"{label}-{model_id}",
        model_id=model_id,
        base_url=base_url.rstrip("/"),
        api_key_env=api_key_env,
        timeout=timeout,
        transport=transport,
    )


def free_tier_model_from_env(*, transport: Any = None) -> OpenAICompatModel | None:
    """Configure a free-tier OpenAI-compatible provider from env if present.

    Resolution order:
      1. Explicit CELA_PROVIDER_BASE_URL + CELA_PROVIDER_MODEL + key env.
      2. First present of GEMINI_API_KEY -> GROQ_API_KEY -> OPENROUTER_API_KEY.

    Secrets remain in ``os.environ`` and are referenced only via ``api_key_env``
    (ADR-0011). Returns None if no valid provider configuration exists.
    """
    explicit = _provider_from_env_explicit(transport=transport)
    if explicit is not None:
        return explicit
    candidates = (
        (
            "GEMINI_API_KEY",
            "gemini-2.5-flash",
            os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"),
        ),
        (
            "GROQ_API_KEY",
            "groq-llama-3.3-70b",
            os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
            os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        ),
        (
            "OPENROUTER_API_KEY",
            "openrouter-free",
            os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
            os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        ),
    )
    for env_var, name, model_id, base_url in candidates:
        if os.environ.get(env_var, "").strip():
            return OpenAICompatModel(
                name=name,
                model_id=model_id,
                base_url=base_url.rstrip("/"),
                api_key_env=env_var,
                timeout=60.0,
                transport=transport,
            )
    return None


def resolve_platform_model(
    *,
    mode: str,
    transport: Any = None,
    require_live: bool = False,
) -> tuple[ChatModel, dict[str, Any]]:
    """Resolve the model for a new platform instance, with fail-closed validation.

    Returns (model, info) where info is a public-safe dict describing the
    resolved configuration (no secrets). If require_live=True and no valid
    provider is configured, raises RuntimeError instead of silently falling
    back to scripted mode.
    """
    fallback: ScriptedModel = (
        demo_planner_model("") if mode == "flow" else demo_react_model("")
    )
    live_enabled = is_live_provider_enabled()
    primary = free_tier_model_from_env(transport=transport) if live_enabled else None

    if require_live and live_enabled and primary is None:
        raise RuntimeError(
            "CELA_USE_LIVE_PROVIDER=1 is set but no valid LLM provider is configured. "
            "Set CELA_PROVIDER_BASE_URL + CELA_PROVIDER_MODEL + the corresponding API key "
            "env var (default: GEMINI_API_KEY), or unset CELA_USE_LIVE_PROVIDER to run "
            "in offline demo mode."
        )

    if primary is not None:
        model: ChatModel = ResilientModel(
            fallback=fallback, primary=primary, label="live_provider"
        )
        info = {
            "mode": "live",
            "kind": "provider_adapter",
            "model_name": primary.name,
            "fallback_to": fallback.name,
        }
    else:
        model = fallback
        info = {
            "mode": "offline_demo",
            "kind": "scripted",
            "model_name": fallback.name,
        }
    return model, info


class ResilientModel:
    """Wraps an optional LLM provider with honest fallback to a deterministic core.

    When ``primary`` is ``None`` or raises (HTTP 4xx/5xx, network error, quota),
    execution switches cleanly to ``fallback`` (a :class:`~os_core.models.ScriptedModel`)
    and records ``fallback_reason`` so ``describe_execution`` honestly reports
    ``kind="scripted"`` instead of pretending an LLM answered.

    Used for both the generic ReAct/Flow CLI (stand-in + real tool calls) and
    Mission OS.
    """

    def __init__(
        self,
        *,
        fallback: ScriptedModel,
        primary: ChatModel | None = None,
        label: str = "live_provider",
    ) -> None:
        self.fallback = fallback
        self.primary = primary
        self.label = label
        self.fallback_triggered: bool = primary is None
        self.fallback_reason: str | None = (
            "no_provider_configured" if primary is None else None
        )

    @property
    def active_model(self) -> ChatModel:
        if self.primary is not None and not self.fallback_triggered:
            return self.primary
        return self.fallback

    @property
    def name(self) -> str:
        return self.active_model.name

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> ModelResponse:
        if self.primary is not None and not self.fallback_triggered:
            try:
                return await self.primary.complete(
                    messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as exc:
                self.fallback_triggered = True
                self.fallback_reason = scrub_credentials(f"{type(exc).__name__}: {exc}")
        return await self.fallback.complete(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )


# Backwards-compatible alias for existing Mission OS callers/tests.
ResilientMissionModel = ResilientModel


def build_mission_platform(
    *,
    root: str = ".",
    project_name: str = "Celia.sh",
    goal: str = "افحص مشروعي، واكتشف نقاط الضعف، ثم أنشئ خطة نشر مجانية وآمنة",
    url: str | None = None,
    url_policy: EgressPolicy | None = None,
    transport: Any = None,
    interactive: bool = False,
    primary_model: ChatModel | None = None,
    use_env_provider: bool = False,
) -> Platform:
    """Build a deterministic Mission OS platform (offline-capable, no API keys)."""
    from os_tools.mission import mission_tools

    registry, _guard, _browser = build_registry()
    registry.extend(mission_tools(root=root, url_policy=url_policy, transport=transport))
    fallback = demo_mission_planner_model(
        project_name=project_name,
        goal=goal,
        path=".",
        url=url,
    )
    chosen_primary = primary_model or (
        free_tier_model_from_env(transport=transport) if use_env_provider else None
    )
    model: ChatModel = (
        ResilientMissionModel(fallback=fallback, primary=chosen_primary)
        if chosen_primary is not None
        else fallback
    )
    return Platform(
        model=model,
        registry=registry,
        config=PlatformConfig(mode="flow", max_steps=8, token_budget=50_000),
        router=ModelRouter(config=RouterConfig(token_budget=50_000)),
        approver=QueueApprover(timeout=60.0) if interactive else QueueApprover(timeout=1.0),
    )
