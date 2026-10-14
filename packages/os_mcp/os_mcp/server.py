"""Serve the platform's tools *to* an MCP host (Claude Desktop, Cursor, Claude Code…).

:class:`MCPRegistryServer` answers ``initialize``, ``ping``, ``tools/list`` and
``tools/call`` for a :class:`~os_tools.registry.ToolRegistry` over stdio. The
policy that the agents apply to every call applies here too, because an
external host is just another caller:

* arguments are screened by :class:`Guardrails` (injection in args → denied),
* the call is risk-classified with the tool's declared level, and a
  ``dangerous`` call goes to the :class:`Approver` — the default
  :class:`DenyApprover` refuses it (fail closed; no human sits on a pipe),
* the result is mapped to MCP content blocks: text, images as ``image``
  blocks, everything else as embedded resources; ``isError`` mirrors the
  platform's status.

``handle()`` is a pure function of one message, so the whole dispatch is
tested without a pipe; ``serve()`` is the newline-delimited loop around it.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import Any

from os_core.guardrails import Guardrails, ScanVerdict
from os_core.hitl import ApprovalDecision, Approver, DenyApprover
from os_core.types import Artifact, ToolCall, ToolResult, ToolStatus
from os_tools.mcp import ToolFilter
from os_tools.registry import ToolRegistry

from os_mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_INITIALIZE,
    METHOD_INITIALIZED,
    METHOD_NOT_FOUND,
    METHOD_PING,
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    MCPProtocolError,
    classify_message,
    decode_message,
    encode_message,
    make_error,
    make_result,
)

SERVER_NAME = "cela_use_browser"
SERVER_VERSION = "0.1.0"
DEFAULT_MAX_LINE_BYTES = 10 * 1024 * 1024


def _artifact_block(artifact: Artifact) -> dict[str, Any]:
    if artifact.kind == "image" and artifact.metadata.get("encoding") == "base64":
        return {"type": "image", "data": artifact.content, "mimeType": artifact.mime_type}
    resource: dict[str, Any] = {
        "uri": f"cela://artifacts/{artifact.filename}",
        "mimeType": artifact.mime_type,
    }
    if artifact.metadata.get("encoding") == "base64":
        resource["blob"] = artifact.content
    else:
        resource["text"] = artifact.content
    return {"type": "resource", "resource": resource}


def result_to_content(result: ToolResult) -> dict[str, Any]:
    """Platform :class:`ToolResult` → MCP ``tools/call`` result."""
    blocks: list[dict[str, Any]] = []
    if result.status is ToolStatus.SUCCESS:
        blocks.append({"type": "text", "text": result.output or "(empty output)"})
    elif result.status is ToolStatus.DENIED:
        blocks.append(
            {"type": "text", "text": f"DENIED by policy: {result.error or 'approval required'}"}
        )
    else:
        blocks.append({"type": "text", "text": f"ERROR: {result.error or 'unknown failure'}"})
    blocks.extend(_artifact_block(a) for a in result.artifacts)
    return {"content": blocks, "isError": result.status is not ToolStatus.SUCCESS}


class MCPRegistryServer:
    """Registry → MCP server. One instance per connection."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        guardrails: Guardrails | None = None,
        approver: Approver | None = None,
        tool_filter: ToolFilter | None = None,
        name: str = SERVER_NAME,
        version: str = SERVER_VERSION,
        instructions: str = "",
    ) -> None:
        self.registry = registry
        self.guardrails = guardrails if guardrails is not None else Guardrails()
        self.approver = approver if approver is not None else DenyApprover()
        self.tool_filter = tool_filter
        self.name = name
        self.version = version
        self.instructions = instructions
        self.initialized = False
        self.protocol_version = PROTOCOL_VERSION
        self.calls = 0
        self.denied = 0

    # -- dispatch --------------------------------------------------------
    def _visible(self, name: str) -> bool:
        return self.tool_filter is None or self.tool_filter.permits(name)

    def tool_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for tool in self.registry.tools.values():
            if not self._visible(tool.name):
                continue
            entries.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.parameters,
                    "annotations": {
                        "readOnlyHint": tool.risk.value == "read_only",
                        "destructiveHint": tool.risk.value == "dangerous",
                        "openWorldHint": tool.risk.value == "network",
                        "title": tool.name,
                    },
                }
            )
        return entries

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """One message in, at most one message out (``None`` for notifications)."""
        kind = classify_message(message)
        if kind == "invalid":
            return make_error(message.get("id"), INVALID_REQUEST, "invalid JSON-RPC message")
        if kind == "response":
            return None  # we never send requests, so there is nothing to match
        method = message["method"]
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if kind == "notification":
            if method == METHOD_INITIALIZED:
                self.initialized = True
            return None

        request_id = message["id"]
        try:
            if method == METHOD_INITIALIZE:
                return make_result(request_id, self._initialize(params))
            if method == METHOD_PING:
                return make_result(request_id, {})
            if method == METHOD_TOOLS_LIST:
                return make_result(request_id, {"tools": self.tool_entries()})
            if method == METHOD_TOOLS_CALL:
                return make_result(request_id, await self._call(params))
            return make_error(request_id, METHOD_NOT_FOUND, f"method not found: {method}")
        except MCPProtocolError as exc:
            return make_error(request_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # never let one call kill the connection
            return make_error(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        wanted = params.get("protocolVersion")
        self.protocol_version = (
            wanted
            if isinstance(wanted, str) and wanted in SUPPORTED_PROTOCOL_VERSIONS
            else PROTOCOL_VERSION
        )
        result: dict[str, Any] = {
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    async def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise MCPProtocolError("tools/call needs a string 'name'")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise MCPProtocolError("tools/call 'arguments' must be an object")
        self.calls += 1
        call = ToolCall(name=name, args=arguments)
        tool = self.registry.get(name)
        if tool is None or not self._visible(name):
            return result_to_content(
                ToolResult(
                    tool_call_id=call.id,
                    tool_name=name,
                    status=ToolStatus.ERROR,
                    error=f"unknown tool '{name}'",
                )
            )

        screen = self.guardrails.screen(call, declared=tool.risk)
        if screen.verdict is not ScanVerdict.CLEAN:
            self.denied += 1
            return result_to_content(
                ToolResult(
                    tool_call_id=call.id,
                    tool_name=name,
                    status=ToolStatus.DENIED,
                    error=screen.detail,
                )
            )
        risk = self.guardrails.classify(call, declared=tool.risk)
        if self.guardrails.requires_approval(risk):
            decision = await self.approver.request(
                call, risk, f"tool '{name}' is classified {risk.value} (via MCP)"
            )
            if decision is not ApprovalDecision.APPROVED:
                self.denied += 1
                return result_to_content(
                    ToolResult(
                        tool_call_id=call.id,
                        tool_name=name,
                        status=ToolStatus.DENIED,
                        error=f"'{name}' is {risk.value}; approval denied",
                    )
                )
        result = await self.registry.execute(call)
        return result_to_content(result)

    # -- stdio loop ------------------------------------------------------
    async def serve(self, reader: Any, writer: Any) -> None:
        """Run until EOF on *reader*. ``reader.readline()`` / ``writer.write()`` + ``drain()``."""
        while True:
            try:
                line = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                writer.write(encode_message(make_error(None, PARSE_ERROR, "line too long")))
                await writer.drain()
                continue
            if not line:
                return
            try:
                message = decode_message(line)
            except MCPProtocolError as exc:
                writer.write(encode_message(make_error(None, PARSE_ERROR, str(exc))))
                await writer.drain()
                continue
            if message is None:
                continue
            reply = await self.handle(message)
            if reply is not None:
                writer.write(encode_message(reply))
                await writer.drain()


async def serve_stdio(
    server: MCPRegistryServer, *, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
) -> None:
    """Attach *server* to this process's stdin/stdout (binary, line-buffered)."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=max_line_bytes)
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    write_transport, write_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(write_transport, write_protocol, None, loop)
    try:
        await server.serve(reader, writer)
    finally:
        with contextlib.suppress(Exception):
            writer.close()


__all__ = [
    "SERVER_NAME",
    "SERVER_VERSION",
    "MCPRegistryServer",
    "result_to_content",
    "serve_stdio",
]
