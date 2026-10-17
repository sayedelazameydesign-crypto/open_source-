"""Sub-agents (ADR-0020) — config mandates, tool allow-list, resource gate, delegation."""

from __future__ import annotations

from os_agents.subagents import (
    SubAgent,
    SubAgentConfig,
    SubAgentHub,
    hub_from_directory,
    load_sub_agent,
    load_sub_agents,
    restricted_registry,
    subagent_tools,
)
from os_core.models import ModelResponse, ScriptedModel
from os_core.types import ToolCall
from os_tools.files import file_reader_tool
from os_tools.registry import ToolRegistry

PLAN_STYLE_CONFIG = """\
# Explorer Sub-Agent Config
model: "llama3:8b"
role: "Search and repository exploration"
allowed_tools: ["file_reader", "grep_search"]
required_ram_gb: 4
"""


def test_load_plan_style_markdown_config(tmp_path) -> None:
    path = tmp_path / "explorer.md"
    path.write_text(PLAN_STYLE_CONFIG, encoding="utf-8")
    config = load_sub_agent(path)
    assert config.name == "explorer"
    assert config.model == "llama3:8b"
    assert config.allowed_tools == ["file_reader", "grep_search"]
    assert config.required_ram_gb == 4
    assert config.role == "Search and repository exploration"


def test_load_json_config(tmp_path) -> None:
    path = tmp_path / "auditor.json"
    path.write_text(
        '{"model": "deepseek-r1:1.5b", "allowed_tools": ["grep_search"], "max_steps": 3}',
        encoding="utf-8",
    )
    config = load_sub_agent(path)
    assert config.name == "auditor"
    assert config.allowed_tools == ["grep_search"]
    assert config.max_steps == 3


def test_directory_load_skips_readme_and_unknown_suffixes(tmp_path) -> None:
    (tmp_path / "explorer.md").write_text(PLAN_STYLE_CONFIG, encoding="utf-8")
    (tmp_path / "README.md").write_text("# docs, not a mandate\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not a config\n", encoding="utf-8")
    roster = load_sub_agents(tmp_path)
    assert set(roster) == {"explorer"}


def test_restricted_registry_shows_only_allowed_tools(tmp_path) -> None:
    parent = ToolRegistry()
    parent.add(file_reader_tool(tmp_path))
    from os_tools.files import grep_search_tool

    parent.add(grep_search_tool(tmp_path))
    only_grep = restricted_registry(parent, ["grep_search", "file_write"])  # file_write: a typo
    assert only_grep.names() == ["grep_search"]


async def test_denied_tool_is_refused_and_recorded(tmp_path) -> None:
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.add(file_reader_tool(tmp_path))
    model = ScriptedModel(
        script=[
            ModelResponse(tool_calls=[ToolCall(name="python_execute", args={"code": "1"})]),
            ModelResponse(content="done"),
        ]
    )
    sub = SubAgent(
        config=SubAgentConfig(name="explorer", allowed_tools=["file_reader"], required_ram_gb=0.0),
        model=model,
        registry=registry,
    )
    result = await sub.run("look around")
    assert result.ok
    assert "python_execute" in result.denied_tools
    assert result.tool_results == 1
    assert result.stopped_reason == "final answer produced"


async def test_resource_gate_blocks_launch(tmp_path) -> None:
    registry = ToolRegistry()
    registry.add(file_reader_tool(tmp_path))
    model = ScriptedModel(final_content="never reached")
    sub = SubAgent(
        config=SubAgentConfig(name="explorer", required_ram_gb=1024.0),
        model=model,
        registry=registry,
    )
    result = await sub.run("look around")
    assert not result.ok
    assert result.stopped_reason == "resource shortage"
    assert "RAM" in (result.error or "")
    assert model.call_count == 0  # the gate tripped before any inference


async def test_step_bound_reports_partial_answer(tmp_path) -> None:
    registry = ToolRegistry()
    registry.add(file_reader_tool(tmp_path))
    model = ScriptedModel(
        script=[
            ModelResponse(
                content="so far: nothing",
                tool_calls=[ToolCall(name="file_reader", args={"path": "x"})],
            )
        ]
        * 3
    )
    sub = SubAgent(
        config=SubAgentConfig(
            name="explorer", allowed_tools=["file_reader"], max_steps=1, required_ram_gb=0.0
        ),
        model=model,
        registry=registry,
    )
    result = await sub.run("look around")
    assert not result.ok
    assert result.stopped_reason == "step bound"
    assert result.answer == "so far: nothing"  # salvaged, not erased


async def test_happy_path_reads_and_reports(tmp_path) -> None:
    (tmp_path / "notes.md").write_text("# Runbook\n\nRestart the gateway.\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.add(file_reader_tool(tmp_path))
    model = ScriptedModel(
        script=[
            ModelResponse(tool_calls=[ToolCall(name="file_reader", args={"path": "notes.md"})]),
            ModelResponse(content="The runbook says: restart the gateway."),
        ]
    )
    sub = SubAgent(
        config=SubAgentConfig(name="explorer", allowed_tools=["file_reader"], required_ram_gb=0.0),
        model=model,
        registry=registry,
    )
    result = await sub.run("what does the runbook say?")
    assert result.ok
    assert "restart the gateway" in result.answer
    assert result.resource is not None and result.resource.ok


async def test_hub_delegation_and_tools(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    configs = tmp_path / "configs"
    configs.mkdir()
    # The CI host may hold less free RAM than a local Llama needs — the gate
    # (proven elsewhere) would rightly refuse, so this roster asks for none.
    (configs / "explorer.md").write_text(
        PLAN_STYLE_CONFIG.replace("required_ram_gb: 4", "required_ram_gb: 0"),
        encoding="utf-8",
    )
    # Override the model factory so no local server is needed in tests.
    scripted = ScriptedModel(final_content="found alpha")
    hub = hub_from_directory(
        configs,
        root=tmp_path,
        model_factory=lambda cfg: scripted,
    )
    assert isinstance(hub, SubAgentHub)
    assert hub.names() == ["explorer"]

    tools = subagent_tools(hub)
    registry = ToolRegistry()
    registry.extend(tools)
    listed = await registry.execute(ToolCall(name="subagent_list", args={}))
    assert listed.ok and "explorer" in listed.output

    delegated = await registry.execute(
        ToolCall(name="delegate_subagent", args={"task": "find alpha", "agent": "explorer"})
    )
    assert delegated.ok, delegated.error
    assert "found alpha" in delegated.output

    unknown = await registry.execute(
        ToolCall(name="delegate_subagent", args={"task": "x", "agent": "ghost"})
    )
    assert not unknown.ok
    assert "unknown sub-agent" in (unknown.error or "")


async def test_config_file_missing_tool_is_visible(tmp_path) -> None:
    """A config that names a tool the platform does not have must not silently
    grant it — the roster shows a smaller allow-list instead."""
    registry = ToolRegistry()
    registry.add(file_reader_tool(tmp_path))
    sub = SubAgent(
        config=SubAgentConfig(
            name="typo", allowed_tools=["file_reader", "file_write"], required_ram_gb=0.0
        ),
        model=ScriptedModel(final_content="ok"),
        registry=restricted_registry(registry, ["file_reader", "file_write"]),
    )
    assert [t["function"]["name"] for t in sub.specs()] == ["file_reader"]


async def test_registry_wiring_is_env_switched(tmp_path, monkeypatch) -> None:
    """The presence of CELA_SUBAGENTS_DIR *is* the switch — and its absence
    must leave the default registry exactly as it was before ADR-0020."""
    from os_server.demo import build_registry

    monkeypatch.delenv("CELA_SUBAGENTS_DIR", raising=False)
    plain, _, _ = build_registry()
    assert not any(n in plain.names() for n in ("delegate_subagent", "subagent_list"))

    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "explorer.md").write_text(
        PLAN_STYLE_CONFIG.replace("required_ram_gb: 4", "required_ram_gb: 0"), encoding="utf-8"
    )
    monkeypatch.setenv("CELA_SUBAGENTS_DIR", str(configs))
    monkeypatch.setenv("CELA_SUBAGENTS_ROOT", str(tmp_path))
    wired, _, _ = build_registry()
    assert "delegate_subagent" in wired.names()
    assert "subagent_list" in wired.names()
