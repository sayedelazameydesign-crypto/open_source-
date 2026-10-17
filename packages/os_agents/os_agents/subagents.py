"""Sub-agents — a cheap local model doing the walking, a smarter one doing the thinking.

Phase 1 of the local-automation plan (ADR-0020). The split is economic before
it is architectural: repository exploration is high-volume, low-difficulty
work, and every token of it spent on the planner tier is a token wasted. So a
:class:`SubAgent` is a *bounded* loop over a *restricted* tool set — by default
``file_reader`` and ``grep_search`` and nothing else — while the main agent
(Planner / Orchestrator) keeps plan-making and stays free to disagree.

Three hard boundaries, all enforced in code rather than prompt text:

1. **Tool allow-list** — a call outside ``allowed_tools`` is denied before the
   registry sees it, so a prompt-injected sub-agent still cannot write files.
2. **Resource preflight** — :func:`~os_core.resources.check_local_resources`
   runs before the first inference call; an under-provisioned machine gets a
   failed result instead of an OOM kill mid-trajectory.
3. **Bounds** — ``max_steps`` × ``max_seconds``. A sub-agent that cannot finish
   in bounds reports what it has; it does not negotiate for more.

Configs live in ``agents/sub_agents/`` as JSON *or* the plan's ``key: value``
Markdown form (both parsed here), and the layer is wired to the orchestrator
the same way memory and corpus tools are: the presence of a config directory
*is* the switch (``CELA_SUBAGENTS_DIR``), nothing is enabled by default.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from os_core.models import ChatModel, OpenAICompatModel
from os_core.resources import (
    LocalResourceError,
    ResourceCheck,
    check_local_resources,
)
from os_core.types import Message, ToolCall, ToolResult, ToolStatus
from os_tools.registry import Tool, ToolRegistry
from pydantic import BaseModel, Field

#: Where a local Ollama (or any OpenAI-compatible server) usually listens.
DEFAULT_LOCAL_BASE_URL = "http://localhost:11434/v1"

SUB_AGENT_PROMPT = (
    "You are a bounded sub-agent inside a larger orchestration platform. Your "
    "job is data gathering, not decisions: explore, read, and report.\n"
    "- You may ONLY use these tools: {allowed}.\n"
    "- Never request a tool outside that list; such a request is denied.\n"
    "- Work read-only: no edits, no shell, no network.\n"
    "- When you have the answer, reply with a concise report and request no tool."
)


class SubAgentConfig(BaseModel):
    """One sub-agent's mandate. The allow-list is the mandate's hard edge."""

    name: str = "explorer"
    model: str = "llama3:8b"  # a *small* local model — that is the point
    base_url: str = DEFAULT_LOCAL_BASE_URL
    role: str = "Search and repository exploration"
    description: str = ""
    allowed_tools: list[str] = Field(default_factory=lambda: ["file_reader", "grep_search"])
    required_ram_gb: float = 4.0
    required_cpus: int = 1
    max_steps: int = 6
    max_seconds: float = 120.0
    temperature: float = 0.2
    max_tokens: int = 1024


class SubAgentResult(BaseModel):
    """What the orchestrator receives. Failures are results, not exceptions —
    the main agent can fall back to doing the task itself."""

    agent: str
    task: str
    ok: bool
    answer: str = ""
    stopped_reason: str = ""
    steps: int = 0
    tool_results: int = 0
    denied_tools: list[str] = Field(default_factory=list)
    error: str | None = None
    elapsed_seconds: float = 0.0
    resource: ResourceCheck | None = None


# -- config loading ---------------------------------------------------------


def _parse_simple_config(text: str) -> dict[str, Any]:
    """Parse the plan's ``key: value`` config form (JSON values, ``#`` comments).

    Deliberately not YAML: the config surface is a flat key/value list, and a
    mini-parser for exactly that is auditable at a glance — no dependency and
    no implicit typing rules beyond what JSON already defines.
    """
    data: dict[str, Any] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        try:
            data[key] = json.loads(value)
        except json.JSONDecodeError:
            data[key] = value.strip("\"'")
    return data


def load_sub_agent(path: str | Path) -> SubAgentConfig:
    """Load one config file — ``.json`` wholesale, anything else as key/value."""
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    raw = json.loads(text) if target.suffix.lower() == ".json" else _parse_simple_config(text)
    if not isinstance(raw, dict):
        raise ValueError(f"{target}: config must be a mapping, got {type(raw).__name__}")
    raw.setdefault("name", target.stem)
    return SubAgentConfig.model_validate(raw)


def load_sub_agents(directory: str | Path) -> dict[str, SubAgentConfig]:
    """Load every ``.json`` / ``.md`` config in *directory*, keyed by name."""
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"not a directory: {directory}")
    roster: dict[str, SubAgentConfig] = {}
    for path in sorted(root.iterdir()):
        if path.suffix.lower() not in {".json", ".md"} or not path.is_file():
            continue
        if path.stem.lower() == "readme":
            continue  # the directory's own documentation is not a mandate
        config = load_sub_agent(path)
        roster[config.name] = config
    return roster


def local_model_for(config: SubAgentConfig) -> OpenAICompatModel:
    """Default model binding: the config's local endpoint (Ollama-shaped)."""
    return OpenAICompatModel(
        name=f"subagent:{config.name}",
        model_id=config.model,
        base_url=config.base_url,
    )


# -- the sub-agent ----------------------------------------------------------


class SubAgent(BaseModel):
    """One bounded read-only worker over an allow-listed tool set."""

    config: SubAgentConfig
    model: ChatModel
    registry: ToolRegistry

    model_config = {"arbitrary_types_allowed": True}

    def specs(self) -> list[dict[str, Any]]:
        return self.registry.specs()

    async def run(self, task: str) -> SubAgentResult:
        started = time.monotonic()
        cfg = self.config

        # Resource gate first: the plan's rule is "never launch a local model
        # the machine cannot host", and a failed check is a cheap, explainable
        # outcome for the orchestrator to route around.
        try:
            resources = check_local_resources(cfg.required_ram_gb, cfg.required_cpus)
        except LocalResourceError as exc:
            return SubAgentResult(
                agent=cfg.name,
                task=task,
                ok=False,
                stopped_reason="resource shortage",
                error=str(exc),
                elapsed_seconds=time.monotonic() - started,
            )

        allowed = [t.name for t in self.registry.tools.values() if t.name in set(cfg.allowed_tools)]
        messages = [
            Message(
                role="system",
                content=SUB_AGENT_PROMPT.format(allowed=", ".join(cfg.allowed_tools) or "none"),
            ),
            Message(role="user", content=task),
        ]
        result = SubAgentResult(agent=cfg.name, task=task, ok=False, resource=resources)
        denied: list[str] = []
        last_assistant = ""

        for step in range(1, cfg.max_steps + 1):
            result.steps = step
            if time.monotonic() - started > cfg.max_seconds:
                result.stopped_reason = "time bound"
                result.error = f"sub-agent exceeded {cfg.max_seconds:.0f}s without a final answer"
                break

            response = await self.model.complete(
                messages,
                tools=self.specs() or None,
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
            )
            if response.content:
                messages.append(Message(role="assistant", content=response.content))
                last_assistant = response.content

            if not response.wants_tools:
                result.ok = True
                result.answer = response.content
                result.stopped_reason = "final answer produced"
                break

            for call in response.tool_calls:
                tool_result = await self._execute(call, allowed)
                if tool_result.status is ToolStatus.DENIED and call.name not in denied:
                    denied.append(call.name)
                result.tool_results += 1
                messages.append(
                    Message(role="tool", content=tool_result.observation(), tool_call_id=call.id)
                )
        else:
            result.stopped_reason = "step bound"
            result.error = f"sub-agent hit its {cfg.max_steps}-step bound before a final answer"
            # Salvage: whatever the sub-agent last said is still evidence the
            # orchestrator can use — a bounded stop reports, it does not erase.
            result.answer = last_assistant

        result.denied_tools = denied
        result.elapsed_seconds = time.monotonic() - started
        return result

    async def _execute(self, call: ToolCall, allowed: list[str]) -> ToolResult:
        if call.name not in set(self.config.allowed_tools):
            return ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                status=ToolStatus.DENIED,
                error=(
                    f"tool '{call.name}' is not allowed for sub-agent "
                    f"'{self.config.name}' (allowed: {', '.join(self.config.allowed_tools)})"
                ),
            )
        return await self.registry.execute(call)


# -- the orchestrator side --------------------------------------------------


class SubAgentHub(BaseModel):
    """The roster the orchestrator delegates to. Planning stays upstairs."""

    agents: dict[str, SubAgent] = Field(default_factory=dict)
    default_agent: str = ""

    model_config = {"arbitrary_types_allowed": True}

    def names(self) -> list[str]:
        return sorted(self.agents)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": sub.config.name,
                "model": sub.config.model,
                "role": sub.config.role,
                "allowed_tools": list(sub.config.allowed_tools),
            }
            for sub in self.agents.values()
        ]

    def pick(self, agent: str = "") -> SubAgent:
        if agent:
            if agent not in self.agents:
                raise ValueError(
                    f"unknown sub-agent {agent!r}; available: {', '.join(self.names()) or 'none'}"
                )
            return self.agents[agent]
        if self.default_agent and self.default_agent in self.agents:
            return self.agents[self.default_agent]
        if not self.agents:
            raise ValueError("no sub-agents configured")
        return self.agents[self.names()[0]]

    async def delegate(self, task: str, *, agent: str = "") -> SubAgentResult:
        if not task.strip():
            raise ValueError("task is required")
        return await self.pick(agent).run(task)


def restricted_registry(parent: ToolRegistry, allowed: list[str]) -> ToolRegistry:
    """A registry holding only *allowed* tools from *parent*.

    The allow-list is applied at the registry boundary as well as the call
    boundary: the sub-model is never even *shown* a tool it may not use, and a
    config typo (``file_write`` instead of ``file_reader``) shows up here as a
    missing tool rather than as a capability that silently works.
    """
    registry = ToolRegistry(default_timeout=parent.default_timeout)
    for name in allowed:
        tool = parent.get(name)
        if tool is not None:
            registry.add(tool)
    return registry


def hub_from_directory(
    directory: str | Path,
    *,
    root: str | Path = ".",
    registry: ToolRegistry | None = None,
    model_factory: Callable[[SubAgentConfig], ChatModel] | None = None,
) -> SubAgentHub:
    """Build a hub from a config directory over *root*'s read-only file tools.

    When *registry* is omitted, the roster gets its default hands —
    ``file_reader`` and ``grep_search`` confined to *root* — which is the
    plan's explorer configuration end to end.
    """
    from os_tools.files import file_reader_tool, grep_search_tool

    parent = registry if registry is not None else ToolRegistry()
    if registry is None:
        parent.add(file_reader_tool(root))
        parent.add(grep_search_tool(root))

    make_model = model_factory or local_model_for
    agents: dict[str, SubAgent] = {}
    for name, config in load_sub_agents(directory).items():
        agents[name] = SubAgent(
            config=config,
            model=make_model(config),
            registry=restricted_registry(parent, config.allowed_tools),
        )
    return SubAgentHub(agents=agents, default_agent=next(iter(agents), ""))


def subagent_tools(hub: SubAgentHub) -> list[Tool]:
    """The orchestrator's delegation surface: list the roster, delegate a task.

    Registered onto the *main* agent's registry like ``memory_*``/``know_*``
    are — the orchestrator plans and directs, the sub-agent gathers. Both tools
    are READ_ONLY because everything beneath them is.
    """
    from os_core.types import RiskLevel

    def subagent_list() -> dict[str, Any]:
        """List configured sub-agents (name, model, role, allowed tools)."""
        return {"agents": hub.describe()}

    async def delegate_subagent(task: str, agent: str = "") -> dict[str, Any]:
        """Delegate a read-only exploration task to a local sub-agent."""
        result = await hub.delegate(task, agent=agent)
        return {
            "agent": result.agent,
            "ok": result.ok,
            "answer": result.answer,
            "stopped_reason": result.stopped_reason,
            "steps": result.steps,
            "tool_results": result.tool_results,
            "denied_tools": result.denied_tools,
            "error": result.error,
        }

    return [
        Tool(
            name="subagent_list",
            description="List the configured sub-agents and what each is allowed to do.",
            parameters={"type": "object", "properties": {}},
            risk=RiskLevel.READ_ONLY,
            fn=subagent_list,
        ),
        Tool(
            name="delegate_subagent",
            description=(
                "Delegate a bounded read-only exploration task (search/read/report) "
                "to a local sub-agent. Planning stays with you; this is for data gathering."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "the exploration task"},
                    "agent": {
                        "type": "string",
                        "description": "sub-agent name (default: the hub's default)",
                    },
                },
                "required": ["task"],
            },
            risk=RiskLevel.READ_ONLY,
            fn=delegate_subagent,
        ),
    ]


__all__ = [
    "DEFAULT_LOCAL_BASE_URL",
    "SubAgent",
    "SubAgentConfig",
    "SubAgentHub",
    "SubAgentResult",
    "hub_from_directory",
    "load_sub_agent",
    "load_sub_agents",
    "local_model_for",
    "restricted_registry",
    "subagent_tools",
]
