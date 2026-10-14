"""The MCP layer — configured servers, their health, and the tools they yield.

One :class:`MCPLayer` per process (the server builds a platform per run, so a
layer per platform could never keep a child process alive). ``discover()``
connects every enabled server, lists its tools and bridges them; a server
that cannot be reached is reported as ``off`` with its reason and contributes
nothing — never a crash, never a tool that silently does not work.

Wiring is explicit, as for every outside-world capability in this platform:
nothing happens unless ``CELA_MCP_ENABLE=1`` *and* a config file names at
least one server.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any

from os_core.guardrails import Guardrails
from os_reach.security import scrub_credentials
from os_tools.egress import EgressPolicy
from os_tools.registry import Tool, ToolRegistry
from pydantic import BaseModel, Field

from os_mcp.bridge import BridgeReport, bridge_tools
from os_mcp.client import BaseMCPClient, HttpMCPClient, StdioMCPClient, policy_for_endpoint
from os_mcp.config import (
    MCP_CONFIG_ENV,
    MCP_ENABLE_ENV,
    MCPConfig,
    MCPConfigError,
    MCPServerConfig,
    load_mcp_config,
)
from os_mcp.protocol import MCPError

ClientFactory = Callable[[MCPServerConfig], BaseMCPClient]


class ServerState(StrEnum):
    OK = "ok"
    OFF = "off"
    ERROR = "error"


class ServerHealth(BaseModel):
    name: str
    transport: str
    status: ServerState
    message: str = ""
    tools: int = 0
    quarantined: int = 0
    protocol_version: str | None = None
    server_info: dict[str, str] = Field(default_factory=dict)


def mcp_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get(MCP_ENABLE_ENV) == "1"


def default_client_factory(
    *, resolve_dns: bool = False, http_transport: Any = None
) -> ClientFactory:
    """Build the real clients from a config entry.

    *http_transport* is the httpx transport injection seam used by tests; the
    egress policy for an HTTP server admits exactly its configured origin.
    """

    def factory(config: MCPServerConfig) -> BaseMCPClient:
        if config.transport == "stdio":
            assert config.command is not None
            return StdioMCPClient(
                server_name=config.name,
                command=config.command,
                args=config.args,
                env=config.child_env(),
                cwd=config.cwd,
                timeout=config.timeout,
                max_chars=config.max_chars,
            )
        assert config.url is not None
        label = f"mcp_{config.name}"
        policy: EgressPolicy = policy_for_endpoint(config.url, tool=label, resolve_dns=resolve_dns)
        return HttpMCPClient(
            server_name=config.name,
            url=config.url,
            headers=config.resolved_headers,  # resolved at request time (ADR-0011)
            policy=policy,
            timeout=config.timeout,
            max_chars=config.max_chars,
            transport=http_transport,
        )

    return factory


class MCPLayer:
    """Owns the clients for every configured server and the tools they bridge."""

    def __init__(
        self,
        config: MCPConfig,
        *,
        guardrails: Guardrails | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.config = config
        self.guardrails = guardrails if guardrails is not None else Guardrails()
        self.client_factory = client_factory or default_client_factory()
        self.clients: dict[str, BaseMCPClient] = {}
        self.health: dict[str, ServerHealth] = {}
        self.reports: dict[str, BridgeReport] = {}
        self._tools: list[Tool] = []
        self.discovered = False

    # -- construction ----------------------------------------------------
    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        guardrails: Guardrails | None = None,
        client_factory: ClientFactory | None = None,
    ) -> MCPLayer:
        """Layer from ``CELA_MCP_CONFIG``; a broken file becomes a note, not a crash."""
        source = os.environ if env is None else env
        try:
            config = load_mcp_config(source.get(MCP_CONFIG_ENV), env=source)
        except MCPConfigError as exc:
            config = MCPConfig(notes=[f"config rejected: {exc}"])
        return cls(config, guardrails=guardrails, client_factory=client_factory)

    @classmethod
    def disabled(cls, reason: str) -> MCPLayer:
        return cls(MCPConfig(notes=[reason]))

    # -- lifecycle -------------------------------------------------------
    async def discover(self) -> dict[str, ServerHealth]:
        """Connect every enabled server, list and bridge its tools.

        Idempotent per process: calling it again closes and re-discovers,
        which is how a ``tools/list_changed`` notification is honoured.
        """
        if self.discovered:
            await self.close()
        taken: set[str] = set()
        for server in self.config.servers:
            if not server.enabled:
                self.health[server.name] = ServerHealth(
                    name=server.name,
                    transport=server.transport,
                    status=ServerState.OFF,
                    message=server.disabled_reason or "disabled",
                )
                continue
            await self._discover_one(server, taken)
        self.discovered = True
        return dict(self.health)

    async def _discover_one(self, server: MCPServerConfig, taken: set[str]) -> None:
        client: BaseMCPClient | None = None
        try:
            client = self.client_factory(server)
            await client.connect()
            specs = await client.tool_specs()
        except MCPError as exc:
            if client is not None:
                await client.close()
            self.health[server.name] = ServerHealth(
                name=server.name,
                transport=server.transport,
                status=ServerState.OFF,
                message=scrub_credentials(str(exc)),
            )
            return
        except Exception as exc:  # a bug in a client must not take the platform down
            if client is not None:
                await client.close()
            self.health[server.name] = ServerHealth(
                name=server.name,
                transport=server.transport,
                status=ServerState.ERROR,
                message=scrub_credentials(f"{type(exc).__name__}: {exc}"),
            )
            return

        tools, report = bridge_tools(
            client, specs, config=server, guardrails=self.guardrails, taken=taken
        )
        self.clients[server.name] = client
        self.reports[server.name] = report
        self._tools.extend(tools)
        self.health[server.name] = ServerHealth(
            name=server.name,
            transport=server.transport,
            status=ServerState.OK,
            message=(
                f"{len(tools)} tool(s) bridged"
                + (f", {len(report.quarantined)} quarantined" if report.quarantined else "")
                + (f", {len(report.filtered)} filtered" if report.filtered else "")
                + ("; " + "; ".join(client.notes) if client.notes else "")
            ),
            tools=len(tools),
            quarantined=len(report.quarantined),
            protocol_version=client.protocol_version,
            server_info=dict(client.server_info),
        )

    async def close(self) -> None:
        for client in list(self.clients.values()):
            await client.close()
        self.clients.clear()
        self.reports.clear()
        self._tools = []
        self.discovered = False

    # -- what the platform consumes --------------------------------------
    def registry_tools(self) -> list[Tool]:
        return list(self._tools)

    def tool_names(self) -> list[str]:
        return sorted(t.name for t in self._tools)

    def is_active(self) -> bool:
        return any(h.status is ServerState.OK for h in self.health.values())

    def report(self) -> dict[str, Any]:
        """JSON-able view for ``cela mcp list|doctor`` and ``GET /api/mcp/servers``."""
        return {
            "config_path": self.config.path,
            "discovered": self.discovered,
            "servers": [s.describe() for s in self.config.servers],
            "health": {name: h.model_dump(mode="json") for name, h in self.health.items()},
            "tools": [
                {
                    "server": b.server,
                    "name": b.name,
                    "registry_name": b.registry_name,
                    "risk": b.risk.value,
                    "raised_by_annotation": b.raised_by_annotation,
                }
                for rep in self.reports.values()
                for b in rep.bridged
            ],
            "quarantined": [
                q.model_dump(mode="json") for rep in self.reports.values() for q in rep.quarantined
            ],
            "filtered": {name: list(rep.filtered) for name, rep in self.reports.items()},
            "clients": {name: c.describe() for name, c in self.clients.items()},
            "notes": list(self.config.notes),
        }

    def render(self) -> str:
        lines = [f"mcp config: {self.config.path or '(none)'}"]
        if not self.config.servers:
            lines.append("  no servers configured")
        for server in self.config.servers:
            health = self.health.get(server.name)
            state = health.status.value if health else "not probed"
            detail = f" — {health.message}" if health and health.message else ""
            lines.append(
                f"  {server.name:<20} {server.transport:<5} risk={server.risk.value:<9} "
                f"{state}{detail}"
            )
            report = self.reports.get(server.name)
            if report is not None:
                for b in report.bridged:
                    flag = " (raised by destructiveHint)" if b.raised_by_annotation else ""
                    lines.append(f"      ✓ {b.registry_name}  risk={b.risk.value}{flag}")
                for q in report.quarantined:
                    lines.append(f"      ✗ {q.name}  QUARANTINED: {', '.join(q.matched)}")
                for name in report.filtered:
                    lines.append(f"      · {name}  filtered by allow/deny")
        for note in self.config.notes:
            lines.append(f"  ! {note}")
        return "\n".join(lines)


def register_mcp_tools(registry: ToolRegistry, layer: MCPLayer) -> list[str]:
    """Add the layer's bridged tools to *registry*; never overwrite an existing name."""
    added: list[str] = []
    for tool in layer.registry_tools():
        if registry.get(tool.name) is not None:
            continue
        registry.add(tool)
        added.append(tool.name)
    return added


_LAYER: MCPLayer | None = None


def process_layer(*, reset: bool = False) -> MCPLayer | None:
    """The process-wide layer when ``CELA_MCP_ENABLE=1``; ``None`` otherwise.

    Shared on purpose (same reasoning as the skill manager in ``os_server.demo``):
    the server builds a platform per run, and a stdio server is a child process
    that must outlive any single run.
    """
    global _LAYER
    if reset:
        _LAYER = None
    if not mcp_enabled():
        return None
    if _LAYER is None:
        _LAYER = MCPLayer.from_env()
    return _LAYER


__all__ = [
    "ClientFactory",
    "MCPLayer",
    "ServerHealth",
    "ServerState",
    "default_client_factory",
    "mcp_enabled",
    "process_layer",
    "register_mcp_tools",
]
