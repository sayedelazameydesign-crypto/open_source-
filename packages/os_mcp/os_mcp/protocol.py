"""MCP wire protocol — JSON-RPC 2.0 framing and the tool-shaped messages we use.

The Model Context Protocol (MCP) is JSON-RPC 2.0 carried over a transport
(newline-delimited stdio, or "Streamable HTTP"). This module owns everything
that is *pure*: encoding/decoding one message, classifying it, the lifecycle
constants, parsing a ``tools/list`` entry, and turning a ``tools/call`` result
into the platform's :class:`~os_core.types.ToolResult` shape. Nothing here does
I/O, so all of it is tested without a socket or a subprocess.

Only the subset of MCP the platform consumes is implemented on purpose:
``initialize``, ``notifications/initialized``, ``ping``, ``tools/list`` and
``tools/call``. Resources, prompts, sampling, roots and elicitation are *not*
supported — and server-initiated requests for them are answered with
``-32601 Method not found`` (see the clients), because a tool server must never
be able to drive our model or read our filesystem.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from typing import Any, Literal

from os_core.types import Artifact
from pydantic import BaseModel, Field

JSONRPC_VERSION = "2.0"

#: The protocol revision we speak. Servers may negotiate down to an older one.
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")

CLIENT_NAME = "cela_use_browser"
CLIENT_VERSION = "0.1.0"

# JSON-RPC 2.0 reserved error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Methods (client -> server unless noted).
METHOD_INITIALIZE = "initialize"
METHOD_INITIALIZED = "notifications/initialized"
METHOD_PING = "ping"
METHOD_TOOLS_LIST = "tools/list"
METHOD_TOOLS_CALL = "tools/call"
NOTIFICATION_TOOLS_CHANGED = "notifications/tools/list_changed"

#: Longest tool name we accept from a server. The spec recommends <= 128; a
#: longer one is almost certainly an attempt to smuggle text past a reviewer.
MAX_TOOL_NAME_CHARS = 128
#: OpenAI-style function names (what the model sees) are limited to 64 chars
#: of ``[A-Za-z0-9_-]``; every bridged name is fitted into that.
MAX_FUNCTION_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1024

MessageKind = Literal["request", "notification", "response", "invalid"]


class MCPError(Exception):
    """Base class for everything this package raises."""


class MCPProtocolError(MCPError):
    """The peer sent something that is not valid JSON-RPC / MCP."""


class MCPRemoteError(MCPError):
    """The server answered a request with a JSON-RPC ``error`` object."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{message} (code {code})")
        self.code = code
        self.message = message
        self.data = data


class MCPTransportError(MCPError):
    """The transport failed: process died, HTTP status, body cap, timeout."""


class MCPTimeoutError(MCPTransportError):
    """A request did not get its response in time."""


class MCPConnectError(MCPTransportError):
    """The server could not be started or the handshake failed."""


class MCPSessionExpired(MCPTransportError):
    """Streamable HTTP: the server no longer knows our ``Mcp-Session-Id``."""


# --------------------------------------------------------------------------- framing


def encode_message(message: dict[str, Any]) -> bytes:
    """One JSON-RPC message as a single UTF-8 line (stdio framing).

    ``json.dumps`` escapes embedded newlines inside strings, so the result is
    guaranteed to contain exactly one ``\\n`` — at the end.
    """
    line = json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str)
    if "\n" in line or "\r" in line:  # pragma: no cover — json.dumps never emits these
        raise MCPProtocolError("message would span several lines")
    return (line + "\n").encode("utf-8")


def decode_message(raw: bytes | str) -> dict[str, Any] | None:
    """Parse one line into a message; ``None`` for a blank line.

    Raises :class:`MCPProtocolError` for anything that is not a JSON object.
    JSON-RPC batches (arrays) are deliberately not accepted on stdio: MCP
    removed them in 2025-06-18 and no server we target emits them.
    """
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MCPProtocolError(f"invalid JSON on the wire: {exc.msg}") from None
    if not isinstance(parsed, dict):
        raise MCPProtocolError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def classify_message(message: dict[str, Any]) -> MessageKind:
    if message.get("jsonrpc") != JSONRPC_VERSION:
        return "invalid"
    has_id = "id" in message and message["id"] is not None
    if "method" in message:
        if not isinstance(message["method"], str):
            return "invalid"
        return "request" if has_id else "notification"
    if has_id and ("result" in message or "error" in message):
        return "response"
    return "invalid"


def make_request(request_id: int | str, method: str, params: dict[str, Any] | None = None) -> dict:
    message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def make_notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        message["params"] = params
    return message


def make_result(request_id: int | str, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def make_error(
    request_id: int | str | None, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def initialize_params(client_name: str = CLIENT_NAME, version: str = CLIENT_VERSION) -> dict:
    """Our ``initialize`` request: no sampling, roots or elicitation capability.

    Advertising none of those is a security statement, not an omission — a
    server cannot ask us to run a model completion or list our files.
    """
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": client_name, "version": version},
    }


def negotiate_protocol_version(offered: Any) -> str:
    """Pick the version to run after an ``initialize`` exchange.

    The server replies with the version it wants. We accept any revision we
    know; an unknown one is a hard failure (the spec says disconnect), surfaced
    with the offered value so the operator can see what the server wanted.
    """
    if not isinstance(offered, str) or offered not in SUPPORTED_PROTOCOL_VERSIONS:
        raise MCPProtocolError(
            f"server wants protocol version {offered!r}; this client supports "
            f"{', '.join(SUPPORTED_PROTOCOL_VERSIONS)}"
        )
    return offered


# --------------------------------------------------------------------------- SSE


def parse_sse(text: str) -> Iterator[dict[str, Any]]:
    """Yield the JSON payload of every ``data:`` event in an SSE body.

    Used by the Streamable HTTP transport when the server answers a POST with
    ``text/event-stream``. Only ``data`` lines matter to us; ``event``, ``id``,
    ``retry`` and comment lines are skipped. Multi-line data is joined with
    ``\\n`` per the SSE spec. Events whose data is not a JSON object are
    dropped rather than failing the whole stream.
    """
    data_lines: list[str] = []
    for raw_line in [*text.splitlines(), ""]:
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    yield parsed
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)


# --------------------------------------------------------------------------- tools


_FUNCTION_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


def bridged_tool_name(server: str, tool: str) -> str:
    """``mcp_<server>_<tool>`` fitted into the function-name charset and length.

    Deterministic: the same server/tool pair always maps to the same name, and
    a name that had to be truncated carries a short hash so two long names do
    not collapse into one.
    """
    raw = f"mcp_{server}_{tool}"
    cleaned = _FUNCTION_NAME_RE.sub("_", raw)
    if len(cleaned) <= MAX_FUNCTION_NAME_CHARS:
        return cleaned
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    keep = MAX_FUNCTION_NAME_CHARS - len(digest) - 1
    return f"{cleaned[:keep]}_{digest}"


class MCPToolSpec(BaseModel):
    """One entry of a ``tools/list`` result, validated."""

    name: str
    title: str = ""
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    annotations: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_wire(cls, raw: Any) -> MCPToolSpec:
        if not isinstance(raw, dict):
            raise MCPProtocolError(f"tool entry must be an object, got {type(raw).__name__}")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise MCPProtocolError("tool entry without a name")
        if len(name) > MAX_TOOL_NAME_CHARS:
            raise MCPProtocolError(f"tool name longer than {MAX_TOOL_NAME_CHARS} characters")
        schema = raw.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        else:
            schema = dict(schema)
            schema.setdefault("type", "object")
            if not isinstance(schema.get("properties"), dict):
                schema["properties"] = {}
            required = schema.get("required")
            schema["required"] = (
                [r for r in required if isinstance(r, str)] if isinstance(required, list) else []
            )
        annotations = raw.get("annotations")
        return cls(
            name=name,
            title=str(raw.get("title") or ""),
            description=str(raw.get("description") or ""),
            input_schema=schema,
            annotations=dict(annotations) if isinstance(annotations, dict) else {},
        )

    def review_text(self) -> str:
        """Everything a server can write that the model would later read.

        The name, title, description *and* the schema (property descriptions
        are a classic place to hide instructions) are scanned together before
        the tool is ever advertised.
        """
        return "\n".join(
            [
                self.name,
                self.title,
                self.description,
                json.dumps(self.input_schema, ensure_ascii=False, sort_keys=True),
            ]
        )

    def declares_destructive(self) -> bool:
        """``annotations.destructiveHint`` set to ``true`` *explicitly*.

        The spec's default for a missing hint is ``true``, which would make
        every un-annotated tool look destructive; only an explicit statement
        by the server counts, and it can only ever *raise* our risk floor.
        """
        return self.annotations.get("destructiveHint") is True


class ToolCallOutcome(BaseModel):
    """A ``tools/call`` result folded into text + artifacts."""

    text: str = ""
    artifacts: list[Artifact] = Field(default_factory=list)
    is_error: bool = False
    blocks: int = 0
    unsupported: list[str] = Field(default_factory=list)


_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "application/json": "json",
    "text/html": "html",
    "text/markdown": "md",
    "text/plain": "txt",
}


def _artifact_kind(mime: str) -> Literal["code", "html", "markdown", "image", "json", "file"]:
    if mime.startswith("image/"):
        return "image"
    if mime == "text/html":
        return "html"
    if mime == "text/markdown":
        return "markdown"
    if mime == "application/json":
        return "json"
    return "file"


def parse_tool_result(raw: Any, *, prefix: str = "mcp") -> ToolCallOutcome:
    """Fold the ``content`` blocks of a ``tools/call`` result.

    * ``text`` blocks become the observation, joined by blank lines.
    * ``image``/``audio`` blocks become base64 artifacts (same encoding the
      browser uses for screenshots).
    * embedded ``resource`` blocks become artifacts: text resources keep their
      text, binary ones are base64.
    * ``resource_link`` blocks are listed by name and URI only — nothing is
      fetched; the agent decides what to do with a link, under its own egress
      policy.
    * ``structuredContent`` is rendered as JSON when there is no text.
    """
    if not isinstance(raw, dict):
        raise MCPProtocolError(f"tools/call result must be an object, got {type(raw).__name__}")
    outcome = ToolCallOutcome(is_error=bool(raw.get("isError", False)))
    content = raw.get("content")
    if content is None:
        content = []
    if not isinstance(content, list):
        raise MCPProtocolError("tools/call result 'content' must be an array")

    texts: list[str] = []
    for index, block in enumerate(content):
        if not isinstance(block, dict):
            outcome.unsupported.append(type(block).__name__)
            continue
        outcome.blocks += 1
        kind = block.get("type")
        if kind == "text":
            texts.append(str(block.get("text", "")))
        elif kind in {"image", "audio"}:
            mime = str(block.get("mimeType") or ("image/png" if kind == "image" else "audio/wav"))
            ext = _MIME_EXT.get(mime, "bin")
            outcome.artifacts.append(
                Artifact(
                    kind="image" if kind == "image" else "file",
                    filename=f"{prefix}_{index}.{ext}",
                    content=str(block.get("data", "")),
                    mime_type=mime,
                    metadata={"encoding": "base64", "source": "mcp"},
                )
            )
            texts.append(f"[{kind} artifact: {prefix}_{index}.{ext}]")
        elif kind == "resource":
            resource = block.get("resource")
            if not isinstance(resource, dict):
                outcome.unsupported.append("resource-without-body")
                continue
            uri = str(resource.get("uri") or f"{prefix}_{index}")
            mime = str(resource.get("mimeType") or "text/plain")
            filename = f"{prefix}_{index}.{_MIME_EXT.get(mime, 'bin')}"
            if "text" in resource:
                outcome.artifacts.append(
                    Artifact(
                        kind=_artifact_kind(mime),
                        filename=filename,
                        content=str(resource.get("text", "")),
                        mime_type=mime,
                        metadata={"uri": uri, "source": "mcp"},
                    )
                )
                texts.append(f"[resource {uri}]\n{resource.get('text', '')}")
            else:
                outcome.artifacts.append(
                    Artifact(
                        kind=_artifact_kind(mime),
                        filename=filename,
                        content=str(resource.get("blob", "")),
                        mime_type=mime,
                        metadata={"uri": uri, "encoding": "base64", "source": "mcp"},
                    )
                )
                texts.append(f"[binary resource {uri} -> {filename}]")
        elif kind == "resource_link":
            name = str(block.get("name") or block.get("title") or "")
            texts.append(f"[resource link: {name} {block.get('uri', '')}]".replace("  ", " "))
        else:
            outcome.unsupported.append(str(kind))
            texts.append(f"[unsupported content block: {kind}]")

    if not texts and isinstance(raw.get("structuredContent"), dict):
        texts.append(json.dumps(raw["structuredContent"], ensure_ascii=False, indent=2))
    outcome.text = "\n\n".join(t for t in texts if t)
    return outcome


__all__ = [
    "CLIENT_NAME",
    "CLIENT_VERSION",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "JSONRPC_VERSION",
    "MAX_DESCRIPTION_CHARS",
    "MAX_FUNCTION_NAME_CHARS",
    "MAX_TOOL_NAME_CHARS",
    "METHOD_INITIALIZE",
    "METHOD_INITIALIZED",
    "METHOD_NOT_FOUND",
    "METHOD_PING",
    "METHOD_TOOLS_CALL",
    "METHOD_TOOLS_LIST",
    "NOTIFICATION_TOOLS_CHANGED",
    "PARSE_ERROR",
    "PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "MCPConnectError",
    "MCPError",
    "MCPProtocolError",
    "MCPRemoteError",
    "MCPSessionExpired",
    "MCPTimeoutError",
    "MCPToolSpec",
    "MCPTransportError",
    "ToolCallOutcome",
    "bridged_tool_name",
    "classify_message",
    "decode_message",
    "encode_message",
    "initialize_params",
    "make_error",
    "make_notification",
    "make_request",
    "make_result",
    "negotiate_protocol_version",
    "parse_sse",
    "parse_tool_result",
]
