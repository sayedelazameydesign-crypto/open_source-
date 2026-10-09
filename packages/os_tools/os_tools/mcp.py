"""Unified tool surface — the MCP seam.

Every capability is exposed through one :class:`MCPServer`-shaped object with
``list_tools`` / ``call_tool`` semantics, so an external MCP client can drive
the platform and the platform can equally consume third-party MCP servers.
The in-process :class:`LocalMCPServer` wraps a :class:`ToolRegistry`; that
keeps the prototype dependency-free while preserving the wire contract.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from os_core.types import ToolCall, ToolResult
from pydantic import BaseModel, Field

from os_tools.registry import ToolRegistry


@runtime_checkable
class MCPServer(Protocol):
    async def list_tools(self) -> list[dict[str, Any]]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...


class LocalMCPServer(BaseModel):
    """In-process MCP server over a ToolRegistry."""

    registry: ToolRegistry
    server_name: str = "open-source-platform"
    version: str = "0.1.0"

    async def list_tools(self) -> list[dict[str, Any]]:
        return self.registry.specs()

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        call = ToolCall(name=name, args=arguments or {})
        return await self.registry.execute(call)

    def describe(self) -> dict[str, Any]:
        return {
            "server": self.server_name,
            "version": self.version,
            "tools": self.registry.names(),
        }


class ToolFilter(BaseModel):
    """Allow/deny list applied before tools reach a given agent."""

    allow: set[str] | None = None
    deny: set[str] = Field(default_factory=set)

    def permits(self, name: str) -> bool:
        if name in self.deny:
            return False
        return self.allow is None or name in self.allow

    def filter_specs(self, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [s for s in specs if self.permits(s["function"]["name"])]


def parse_tool_arguments(raw: str | dict[str, Any]) -> dict[str, Any]:
    """Models emit tool args as a JSON string; normalise both shapes."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def build_default_registry(
    *,
    code_backend: Any | None = None,
    browser_tool: Any | None = None,
    ssrf_check: Any | None = None,
):
    """Wire the standard capability set into one registry."""
    from os_tools.browser import browser_tools
    from os_tools.code_exec import LocalSubprocessBackend, python_execute_tool
    from os_tools.registry import ToolRegistry

    registry = ToolRegistry()
    backend = code_backend if code_backend is not None else LocalSubprocessBackend()
    registry.add(python_execute_tool(backend))
    if browser_tool is not None:
        registry.extend(browser_tools(browser_tool, ssrf_check=ssrf_check))
    return registry
