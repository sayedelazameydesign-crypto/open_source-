"""os_mcp — real Model Context Protocol transport for the platform (ADR-0017).

Two directions behind one wire contract:

* **consume**: :class:`StdioMCPClient` / :class:`HttpMCPClient` connect to
  third-party MCP servers; :class:`MCPLayer` reads the ecosystem's
  ``mcpServers`` config, scans and risk-classifies every offered tool, and
  bridges it into the :class:`~os_tools.registry.ToolRegistry` under
  ``mcp_<server>_<tool>``;
* **serve**: :class:`MCPRegistryServer` exposes the platform's own registry to
  any MCP host over stdio, with the same guardrails and HITL gate the agents use.

No new dependency: JSON-RPC framing is stdlib, stdio is ``asyncio`` subprocess
(argv list, no shell), Streamable HTTP is ``httpx`` behind :class:`EgressPolicy`.

Two separate opt-ins, both off by default: ``CELA_MCP_ENABLE=1`` reads the
config and talks to servers; ``CELA_MCP_ALLOW_HOST_EXEC=1`` additionally
allows *stdio* servers — processes started on the host, outside the sandbox
and outside the egress policy. Without the second flag they are listed as
``off`` with the reason, never started.
"""

from __future__ import annotations

from os_mcp.bridge import BridgedTool, BridgeReport, QuarantinedTool, bridge_tools, effective_risk
from os_mcp.client import (
    SANDBOX_MARKER_ENV,
    BaseMCPClient,
    HttpMCPClient,
    StdioMCPClient,
    policy_for_endpoint,
)
from os_mcp.config import (
    CANONICAL_CONFIG_PATH,
    HOST_EXEC_DISABLED_REASON,
    MCP_CONFIG_ENV,
    MCP_ENABLE_ENV,
    MCP_HOST_EXEC_ENV,
    MCPConfig,
    MCPConfigError,
    MCPServerConfig,
    MCPToolFilter,
    default_config_path,
    expand_env,
    host_exec_allowed,
    load_mcp_config,
    parse_mcp_config,
)
from os_mcp.layer import (
    MCPLayer,
    ServerHealth,
    ServerState,
    default_client_factory,
    mcp_enabled,
    process_layer,
    register_mcp_tools,
)
from os_mcp.protocol import (
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    MCPConnectError,
    MCPError,
    MCPProtocolError,
    MCPRemoteError,
    MCPSessionExpired,
    MCPTimeoutError,
    MCPToolSpec,
    MCPTransportError,
    ToolCallOutcome,
    bridged_tool_name,
    parse_sse,
    parse_tool_result,
)
from os_mcp.server import MCPRegistryServer, result_to_content, serve_stdio

__all__ = [
    "CANONICAL_CONFIG_PATH",
    "HOST_EXEC_DISABLED_REASON",
    "MCP_CONFIG_ENV",
    "MCP_ENABLE_ENV",
    "MCP_HOST_EXEC_ENV",
    "PROTOCOL_VERSION",
    "SANDBOX_MARKER_ENV",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "BaseMCPClient",
    "BridgeReport",
    "BridgedTool",
    "HttpMCPClient",
    "MCPConfig",
    "MCPConfigError",
    "MCPConnectError",
    "MCPError",
    "MCPLayer",
    "MCPProtocolError",
    "MCPRegistryServer",
    "MCPRemoteError",
    "MCPServerConfig",
    "MCPSessionExpired",
    "MCPTimeoutError",
    "MCPToolFilter",
    "MCPToolSpec",
    "MCPTransportError",
    "QuarantinedTool",
    "ServerHealth",
    "ServerState",
    "StdioMCPClient",
    "ToolCallOutcome",
    "bridge_tools",
    "bridged_tool_name",
    "default_client_factory",
    "default_config_path",
    "effective_risk",
    "expand_env",
    "host_exec_allowed",
    "load_mcp_config",
    "mcp_enabled",
    "parse_mcp_config",
    "parse_sse",
    "parse_tool_result",
    "policy_for_endpoint",
    "process_layer",
    "register_mcp_tools",
    "result_to_content",
    "serve_stdio",
]
