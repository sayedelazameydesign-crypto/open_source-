"""Bridge — turn a server's ``tools/list`` into registry :class:`Tool` objects.

This is where an external server's claims meet the platform's policy:

1. **Filter first.** The operator's ``tools.allow``/``tools.deny`` lists are
   applied to the *server-side* names before anything else is looked at.
2. **Scan what the server wrote.** Name, title, description *and* the input
   schema go through :meth:`Guardrails.scan_text`. A hit quarantines the tool
   — it is not advertised to the model at all — because a tool description is
   read by the model on every turn, which makes "tool poisoning" a persistent
   injection rather than a one-off one.
3. **Risk is ours, not the server's.** The effective risk is the operator's
   floor for that server (default ``dangerous`` → HITL), optionally refined per
   tool in ``tool_risk``. A server's ``destructiveHint: true`` can *raise* it
   to ``dangerous``; no annotation can ever lower it.
4. **Namespacing.** The model sees ``mcp_<server>_<tool>`` so two servers with
   a ``search`` tool cannot shadow each other, and so the agents' own
   classifier (which keys on names like ``browse`` or ``python_execute``)
   cannot be tricked by a remote tool that calls itself ``python_execute``.
"""

from __future__ import annotations

from typing import Any

from os_core.guardrails import Guardrails
from os_core.types import RiskLevel, ToolResult
from os_tools.registry import Tool
from pydantic import BaseModel, Field

from os_mcp.config import MCPServerConfig
from os_mcp.protocol import MAX_DESCRIPTION_CHARS, MCPToolSpec, bridged_tool_name

_RISK_ORDER = (RiskLevel.READ_ONLY, RiskLevel.NETWORK, RiskLevel.WRITE, RiskLevel.DANGEROUS)


def _max_risk(a: RiskLevel, b: RiskLevel) -> RiskLevel:
    return a if _RISK_ORDER.index(a) >= _RISK_ORDER.index(b) else b


class QuarantinedTool(BaseModel):
    """A tool the server offered that we refused to advertise."""

    server: str
    name: str
    reason: str
    matched: list[str] = Field(default_factory=list)


class BridgedTool(BaseModel):
    server: str
    name: str  # server-side name
    registry_name: str  # what the model sees
    risk: RiskLevel
    raised_by_annotation: bool = False


class BridgeReport(BaseModel):
    """What happened to every tool a server listed."""

    server: str
    bridged: list[BridgedTool] = Field(default_factory=list)
    quarantined: list[QuarantinedTool] = Field(default_factory=list)
    filtered: list[str] = Field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "bridged": [b.model_dump(mode="json") for b in self.bridged],
            "quarantined": [q.model_dump(mode="json") for q in self.quarantined],
            "filtered": list(self.filtered),
        }


def effective_risk(spec: MCPToolSpec, config: MCPServerConfig) -> tuple[RiskLevel, bool]:
    """Operator floor, raised (never lowered) by an explicit destructive hint."""
    floor = config.risk_for(spec.name)
    if spec.declares_destructive():
        raised = _max_risk(floor, RiskLevel.DANGEROUS)
        return raised, raised is not floor
    return floor, False


def _description(spec: MCPToolSpec, config: MCPServerConfig) -> str:
    base = spec.description.strip() or spec.title.strip() or spec.name
    text = f"[MCP:{config.name}] {base}"
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[: MAX_DESCRIPTION_CHARS - 1] + "…"
    return text


def bridge_tools(
    client: Any,
    specs: list[MCPToolSpec],
    *,
    config: MCPServerConfig,
    guardrails: Guardrails | None = None,
    taken: set[str] | None = None,
) -> tuple[list[Tool], BridgeReport]:
    """Build registry tools for *specs* served by *client*.

    *taken* holds registry names already in use (other servers, platform
    tools); a collision is resolved by suffixing, never by overwriting — an
    MCP server must not be able to replace ``python_execute``.
    """
    scanner = guardrails if guardrails is not None else Guardrails()
    used = taken if taken is not None else set()
    report = BridgeReport(server=config.name)
    tools: list[Tool] = []

    for spec in specs:
        if not config.tools.permits(spec.name):
            report.filtered.append(spec.name)
            continue
        scan = scanner.scan_text(spec.review_text())
        if scan.verdict.value != "clean":
            report.quarantined.append(
                QuarantinedTool(
                    server=config.name,
                    name=spec.name,
                    reason="tool description/schema matched injection patterns",
                    matched=list(scan.matched),
                )
            )
            continue

        risk, raised = effective_risk(spec, config)
        registry_name = bridged_tool_name(config.name, spec.name)
        if registry_name in used:
            suffix = 2
            while f"{registry_name[:60]}_{suffix}" in used:
                suffix += 1
            registry_name = f"{registry_name[:60]}_{suffix}"
        used.add(registry_name)

        tools.append(
            _make_tool(
                client,
                spec,
                registry_name=registry_name,
                description=_description(spec, config),
                risk=risk,
                timeout=config.timeout,
            )
        )
        report.bridged.append(
            BridgedTool(
                server=config.name,
                name=spec.name,
                registry_name=registry_name,
                risk=risk,
                raised_by_annotation=raised,
            )
        )
    return tools, report


def _make_tool(
    client: Any,
    spec: MCPToolSpec,
    *,
    registry_name: str,
    description: str,
    risk: RiskLevel,
    timeout: float,
) -> Tool:
    remote_name = spec.name

    async def _call(**kwargs: Any) -> ToolResult:
        return await client.call_tool(remote_name, kwargs)

    return Tool(
        name=registry_name,
        description=description,
        parameters=spec.input_schema,
        risk=risk,
        # The registry's own wait_for sits just above the client's RPC timeout
        # so the client's (more informative) timeout result wins.
        timeout=timeout + 1.0,
        fn=_call,
    )


__all__ = [
    "BridgeReport",
    "BridgedTool",
    "QuarantinedTool",
    "bridge_tools",
    "effective_risk",
]
