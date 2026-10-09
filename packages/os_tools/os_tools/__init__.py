"""os_tools — sandboxed execution engines exposed through an MCP-style surface."""

from __future__ import annotations

from os_tools.browser import (
    BrowserTool,
    FakeBrowser,
    PageSnapshot,
    PlaywrightBrowser,
    RetryPolicy,
    StaleElementError,
    browser_tools,
)
from os_tools.code_exec import (
    CodeExecBackend,
    InMemoryBackend,
    LocalSubprocessBackend,
    SandboxLimits,
    python_execute_tool,
)
from os_tools.mcp import LocalMCPServer, MCPServer, ToolFilter, build_default_registry
from os_tools.registry import Tool, ToolRegistry
from os_tools.ssrf import BlockedURLError, SSRFGuard, URLVerdict

__all__ = [
    "BlockedURLError",
    "BrowserTool",
    "CodeExecBackend",
    "FakeBrowser",
    "InMemoryBackend",
    "LocalMCPServer",
    "LocalSubprocessBackend",
    "MCPServer",
    "PageSnapshot",
    "PlaywrightBrowser",
    "RetryPolicy",
    "SSRFGuard",
    "SandboxLimits",
    "StaleElementError",
    "Tool",
    "ToolFilter",
    "ToolRegistry",
    "URLVerdict",
    "browser_tools",
    "build_default_registry",
    "python_execute_tool",
]
