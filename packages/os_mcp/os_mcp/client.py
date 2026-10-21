"""MCP clients — the two transports behind one :class:`~os_tools.mcp.MCPServer` shape.

Both clients expose ``connect() / list_tools() / call_tool() / close()`` and
satisfy the :class:`os_tools.mcp.MCPServer` protocol, so anything that already
consumes an ``MCPServer`` (the bridge, the memory/knowledge adapters planned
in ADR-0016) works against a real server without knowing the transport.

* :class:`StdioMCPClient` spawns the server as a child process — argv list,
  **no shell**, allow-listed environment — and speaks newline-delimited
  JSON-RPC over its pipes. It refuses to run inside the sandbox
  (``CELA_SANDBOXED=1``), exactly like the reach CLI adapter: sandboxed code
  must not be able to borrow a host capability.
* :class:`HttpMCPClient` speaks Streamable HTTP (2025-03-26+): one endpoint,
  JSON-RPC in POST bodies, responses as JSON or SSE, ``Mcp-Session-Id``
  sessions. Every request is checked against an :class:`EgressPolicy` first,
  on top of the SSRF floor — so a loopback or private endpoint is refused and
  the operator is told to use stdio for local servers.

What neither client will ever do: answer a server-initiated ``sampling``,
``roots`` or ``elicitation`` request (they get ``-32601``), follow a redirect,
or hand the model an unbounded observation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from os_core.redact import scrub_credentials, truncate
from os_core.types import ToolResult, ToolStatus
from os_tools.egress import EgressPolicy, EgressPolicyError, EgressVerdict, parse_http_target
from os_tools.pinned_http import PinnedAsyncHTTPTransport

from os_mcp.protocol import (
    METHOD_INITIALIZE,
    METHOD_INITIALIZED,
    METHOD_NOT_FOUND,
    METHOD_PING,
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
    NOTIFICATION_TOOLS_CHANGED,
    MCPConnectError,
    MCPError,
    MCPProtocolError,
    MCPRemoteError,
    MCPSessionExpired,
    MCPTimeoutError,
    MCPToolSpec,
    MCPTransportError,
    classify_message,
    decode_message,
    encode_message,
    initialize_params,
    make_error,
    make_notification,
    make_request,
    make_result,
    negotiate_protocol_version,
    parse_sse,
    parse_tool_result,
)

SANDBOX_MARKER_ENV = "CELA_SANDBOXED"

DEFAULT_MAX_LINE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_CHARS = 20_000
STDERR_KEEP_BYTES = 4096
#: After this many unparsable lines the peer is not speaking MCP; stop.
MAX_PROTOCOL_ERRORS = 100
MAX_LIST_PAGES = 50


class BaseMCPClient:
    """Handshake, tool listing and result mapping shared by both transports."""

    def __init__(self, *, server_name: str, timeout: float, max_chars: int) -> None:
        self.server_name = server_name
        self.timeout = float(timeout)
        self.max_chars = int(max_chars)
        self.connected = False
        self.protocol_version: str | None = None
        self.capabilities: dict[str, Any] = {}
        self.server_info: dict[str, str] = {}
        #: ``initialize.instructions`` as the server sent it. Recorded for the
        #: doctor report; *never* injected into a prompt — it is untrusted text.
        self.instructions: str = ""
        self.notes: list[str] = []
        self.tools_changed = False
        self.rejected_requests = 0
        self.notifications = 0
        self.calls = 0
        self._next_id = 0

    # -- to be provided by the transport -----------------------------------
    async def _rpc(self, method: str, params: dict[str, Any] | None, *, timeout: float) -> Any:
        raise NotImplementedError

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    async def connect(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    @property
    def transport_name(self) -> str:
        raise NotImplementedError

    # -- shared behaviour ----------------------------------------------------
    def _finish_handshake(self, result: Any) -> None:
        if not isinstance(result, dict):
            raise MCPProtocolError("initialize result must be an object")
        self.protocol_version = negotiate_protocol_version(result.get("protocolVersion"))
        capabilities = result.get("capabilities")
        self.capabilities = dict(capabilities) if isinstance(capabilities, dict) else {}
        info = result.get("serverInfo")
        if isinstance(info, dict):
            self.server_info = {
                "name": str(info.get("name", "")),
                "version": str(info.get("version", "")),
            }
        instructions = result.get("instructions")
        self.instructions = instructions[:4000] if isinstance(instructions, str) else ""
        if "tools" not in self.capabilities:
            self.notes.append("server does not advertise the 'tools' capability")
        self.connected = True

    async def ping(self) -> bool:
        result = await self._rpc(METHOD_PING, None, timeout=self.timeout)
        return isinstance(result, dict)

    async def list_tools(self) -> list[dict[str, Any]]:
        """Raw ``tools/list`` entries, following ``nextCursor`` pagination."""
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            params = {"cursor": cursor} if cursor else None
            result = await self._rpc(METHOD_TOOLS_LIST, params, timeout=self.timeout)
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise MCPProtocolError("tools/list result must contain a 'tools' array")
            tools.extend(t for t in result["tools"] if isinstance(t, dict))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        else:
            self.notes.append(f"tools/list pagination stopped after {MAX_LIST_PAGES} pages")
        return tools

    async def tool_specs(self) -> list[MCPToolSpec]:
        specs: list[MCPToolSpec] = []
        for raw in await self.list_tools():
            try:
                specs.append(MCPToolSpec.from_wire(raw))
            except MCPProtocolError as exc:
                self.notes.append(f"skipped malformed tool entry: {exc}")
        return specs

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """``tools/call`` mapped onto :class:`ToolResult`; never raises."""
        started = time.monotonic()
        self.calls += 1
        call_id = f"mcp_{uuid.uuid4().hex[:8]}"
        meta: dict[str, Any] = {
            "mcp_server": self.server_name,
            "mcp_tool": name,
            "transport": self.transport_name,
            "untrusted": True,
        }
        try:
            raw = await self._rpc(
                METHOD_TOOLS_CALL,
                {"name": name, "arguments": dict(arguments or {})},
                timeout=self.timeout,
            )
            outcome = parse_tool_result(raw, prefix=f"{self.server_name}_{name}")
        except MCPTimeoutError as exc:
            return ToolResult(
                tool_call_id=call_id,
                tool_name=name,
                status=ToolStatus.TIMEOUT,
                error=scrub_credentials(str(exc)),
                duration_ms=round((time.monotonic() - started) * 1000, 2),
                metadata=meta,
            )
        except MCPRemoteError as exc:
            return ToolResult(
                tool_call_id=call_id,
                tool_name=name,
                status=ToolStatus.ERROR,
                error=scrub_credentials(f"server error {exc.code}: {exc.message}"),
                duration_ms=round((time.monotonic() - started) * 1000, 2),
                metadata=meta,
            )
        except MCPError as exc:
            return ToolResult(
                tool_call_id=call_id,
                tool_name=name,
                status=ToolStatus.ERROR,
                error=scrub_credentials(f"{type(exc).__name__}: {exc}"),
                duration_ms=round((time.monotonic() - started) * 1000, 2),
                metadata=meta,
            )

        text = truncate(scrub_credentials(outcome.text), self.max_chars)
        meta.update(
            {
                "truncated": len(text) < len(outcome.text),
                "blocks": outcome.blocks,
                "unsupported_blocks": outcome.unsupported,
            }
        )
        return ToolResult(
            tool_call_id=call_id,
            tool_name=name,
            status=ToolStatus.ERROR if outcome.is_error else ToolStatus.SUCCESS,
            output="" if outcome.is_error else text,
            error=(text or "tool reported an error") if outcome.is_error else None,
            artifacts=outcome.artifacts,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            metadata=meta,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "server": self.server_name,
            "transport": self.transport_name,
            "connected": self.connected,
            "protocol_version": self.protocol_version,
            "server_info": dict(self.server_info),
            "capabilities": sorted(self.capabilities),
            "has_instructions": bool(self.instructions),
            "calls": self.calls,
            "rejected_server_requests": self.rejected_requests,
            "tools_changed": self.tools_changed,
            "notes": list(self.notes),
        }

    # Server -> client traffic that both transports can see.
    def _handle_incoming(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Handle a request/notification from the server; return a reply if any."""
        kind = classify_message(message)
        if kind == "notification":
            if message.get("method") == NOTIFICATION_TOOLS_CHANGED:
                self.tools_changed = True
            self.notifications += 1
            return None
        if kind == "request":
            method = message.get("method")
            if method == METHOD_PING:
                return make_result(message["id"], {})
            self.rejected_requests += 1
            return make_error(
                message["id"],
                METHOD_NOT_FOUND,
                f"{method} is not supported by this client (no sampling, roots or elicitation)",
            )
        return None


# --------------------------------------------------------------------------- stdio


class StdioMCPClient(BaseMCPClient):
    """Child-process transport: argv list, no shell, allow-listed environment."""

    def __init__(
        self,
        *,
        server_name: str,
        command: str,
        args: list[str] | tuple[str, ...] = (),
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = 60.0,
        startup_timeout: float = 30.0,
        shutdown_grace: float = 3.0,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        max_chars: int = DEFAULT_MAX_CHARS,
        process_env: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(server_name=server_name, timeout=timeout, max_chars=max_chars)
        self.command = command
        self.args = list(args)
        self.env = dict(env) if env is not None else None
        self.cwd = cwd
        self.startup_timeout = float(startup_timeout)
        self.shutdown_grace = float(shutdown_grace)
        self.max_line_bytes = int(max_line_bytes)
        # Where the sandbox marker is read from; injectable for tests.
        self._process_env: Mapping[str, str] = (
            process_env if process_env is not None else os.environ
        )
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._stderr_tail = bytearray()
        self._closed = False
        self._stdout_closed = asyncio.Event()
        self.protocol_errors = 0
        self.late_responses = 0
        self.exit_code: int | None = None

    @property
    def transport_name(self) -> str:
        return "stdio"

    @property
    def stderr_tail(self) -> str:
        return self._stderr_tail.decode("utf-8", errors="replace")

    async def wait_closed(self, timeout: float = 5.0) -> bool:
        """Wait until the server's stdout is closed (crash, kill, or clean exit).

        Event-driven, not a sleep: tests and supervisors use it to observe a
        teardown deterministically. Returns ``False`` on timeout.
        """
        try:
            await asyncio.wait_for(self._stdout_closed.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    def resolve_binary(self) -> str:
        """Absolute path of the executable, or :class:`MCPConnectError`."""
        candidate = self.command
        if not os.path.isabs(candidate):
            found = shutil.which(candidate)
            if found is None:
                raise MCPConnectError(f"binary not found on PATH: {candidate}")
            candidate = found
        if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            raise MCPConnectError(f"not an executable file: {candidate}")
        return candidate

    async def connect(self) -> None:
        if self._process_env.get(SANDBOX_MARKER_ENV) == "1":
            raise MCPConnectError(
                f"refusing to spawn an MCP server inside the sandbox ({SANDBOX_MARKER_ENV}=1)"
            )
        if self._proc is not None:
            raise MCPConnectError("client is already connected")
        binary = self.resolve_binary()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                binary,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
                cwd=self.cwd,
                limit=self.max_line_bytes,
            )
        except (OSError, ValueError) as exc:
            raise MCPConnectError(f"cannot start {binary}: {exc}") from None
        self._closed = False
        self._stdout_closed.clear()
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"mcp-{self.server_name}-out"
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"mcp-{self.server_name}-err"
        )
        try:
            result = await self._rpc(
                METHOD_INITIALIZE, initialize_params(), timeout=self.startup_timeout
            )
            self._finish_handshake(result)
            await self._notify(METHOD_INITIALIZED)
        except MCPError as exc:
            detail = self.stderr_tail.strip()
            await self.close()
            suffix = f" (stderr: {scrub_credentials(detail[-300:])})" if detail else ""
            raise MCPConnectError(f"handshake failed: {exc}{suffix}") from None
        except BaseException:
            await self.close()
            raise

    async def _write(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None or self._closed:
            raise MCPTransportError(f"mcp server '{self.server_name}' is not running")
        data = encode_message(message)
        async with self._write_lock:
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                raise MCPTransportError(f"server stdin closed: {exc}") from None

    async def _rpc(self, method: str, params: dict[str, Any] | None, *, timeout: float) -> Any:
        self._next_id += 1
        request_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._write(make_request(request_id, method, params))
        except MCPTransportError:
            self._pending.pop(request_id, None)
            raise
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._pending.pop(request_id, None)
            raise MCPTimeoutError(f"{method} timed out after {timeout:g}s") from None

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write(make_notification(method, params))

    def _fail_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    def _kill(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    async def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        stdout = proc.stdout
        try:
            while True:
                try:
                    line = await stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # readline() clears the oversized buffer and resumes, so we
                    # keep draining to EOF after killing the offender — a pipe
                    # nobody reads would make wait() hang (see ADR-0013, Phase 2).
                    self._fail_pending(
                        MCPTransportError(
                            f"server wrote a line longer than {self.max_line_bytes} bytes"
                        )
                    )
                    self._kill()
                    continue
                if not line:
                    break
                try:
                    message = decode_message(line)
                except MCPProtocolError:
                    self.protocol_errors += 1
                    if self.protocol_errors >= MAX_PROTOCOL_ERRORS:
                        self._fail_pending(MCPTransportError("peer is not speaking MCP"))
                        self._kill()
                    continue
                if message is None:
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            self._fail_pending(MCPTransportError(f"reader failed: {type(exc).__name__}: {exc}"))
        finally:
            self._fail_pending(MCPTransportError(f"mcp server '{self.server_name}' closed stdout"))
            self.connected = False
            self._stdout_closed.set()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        kind = classify_message(message)
        if kind == "response":
            future = self._pending.pop(message["id"], None)
            if future is None or future.done():
                self.late_responses += 1
                return
            error = message.get("error")
            if isinstance(error, dict):
                future.set_exception(
                    MCPRemoteError(
                        int(error.get("code", -32000)),
                        str(error.get("message", "unknown error")),
                        error.get("data"),
                    )
                )
            else:
                future.set_result(message.get("result"))
            return
        if kind == "invalid":
            self.protocol_errors += 1
            return
        reply = self._handle_incoming(message)
        if reply is not None:
            with contextlib.suppress(MCPTransportError):
                await self._write(reply)

    async def _drain_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        stderr = proc.stderr
        try:
            while True:
                chunk = await stderr.read(4096)
                if not chunk:
                    break
                self._stderr_tail.extend(chunk)
                if len(self._stderr_tail) > STDERR_KEEP_BYTES:
                    del self._stderr_tail[:-STDERR_KEEP_BYTES]
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover — defensive
            return

    async def close(self) -> None:
        if self._closed and self._proc is None:
            return
        self._closed = True
        self.connected = False
        proc = self._proc
        if proc is not None:
            if proc.stdin is not None:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=self.shutdown_grace)
                except TimeoutError:
                    self._kill()
                    await proc.wait()
            self.exit_code = proc.returncode
        # Let the readers see EOF (process is gone) instead of cancelling them
        # mid-read: a cancelled reader can leave a pipe transport half-open.
        for task in (self._reader_task, self._stderr_task):
            if task is None:
                continue
            try:
                await asyncio.wait_for(task, timeout=self.shutdown_grace)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            except (asyncio.CancelledError, Exception):
                pass
        self._reader_task = None
        self._stderr_task = None
        self._proc = None
        self._fail_pending(MCPTransportError(f"mcp server '{self.server_name}' was closed"))


# --------------------------------------------------------------------------- http


HeaderProvider = Callable[[], Mapping[str, str]]


def policy_for_endpoint(url: str, *, tool: str, resolve_dns: bool = False) -> EgressPolicy:
    """Default-deny policy that admits exactly the configured endpoint origin.

    The operator wrote ``url`` down, so its scheme/host/port are allowed for
    this one tool label — nothing else is, and the SSRF floor still applies
    underneath (loopback, RFC1918 and metadata addresses stay refused).
    """
    scheme, host, port = parse_http_target(url)
    return EgressPolicy.allow_hosts(
        host, schemes={scheme}, ports={port}, tools={tool}, resolve_dns=resolve_dns
    )


class HttpMCPClient(BaseMCPClient):
    """Streamable HTTP transport (single endpoint, POST per message)."""

    def __init__(
        self,
        *,
        server_name: str,
        url: str,
        headers: Mapping[str, str] | HeaderProvider | None = None,
        policy: EgressPolicy | None = None,
        timeout: float = 60.0,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        max_chars: int = DEFAULT_MAX_CHARS,
        transport: Any = None,
    ) -> None:
        super().__init__(server_name=server_name, timeout=timeout, max_chars=max_chars)
        self.url = url
        self._headers = headers if headers is not None else {}
        self.tool_label = f"mcp_{server_name}"
        self.policy = (
            policy if policy is not None else policy_for_endpoint(url, tool=self.tool_label)
        )
        self.max_body_bytes = int(max_body_bytes)
        self.transport = transport
        self.session_id: str | None = None
        self.requests_sent = 0
        self._closed = False

    @property
    def transport_name(self) -> str:
        return "http"

    def _resolve_headers(self) -> dict[str, str]:
        provided = self._headers() if callable(self._headers) else self._headers
        resolved = {str(k): str(v) for k, v in dict(provided).items()}
        for banned in ("content-type", "accept", "mcp-session-id"):
            for key in list(resolved):
                if key.lower() == banned:
                    resolved.pop(key)
        return resolved

    def _enforce_policy(self) -> EgressVerdict:
        try:
            return self.policy.enforce(
                self.url,
                tool=self.tool_label,
                resolve_dns=not isinstance(self.transport, httpx.MockTransport),
            )
        except (EgressPolicyError, ValueError) as exc:
            raise MCPTransportError(
                f"egress policy refused {scrub_credentials(self.url)}: {exc}. "
                "Local MCP servers must run over stdio; remote ones need an allow rule."
            ) from None

    async def connect(self) -> None:
        self.session_id = None
        self.protocol_version = None
        self.connected = False
        try:
            result = await self._rpc(
                METHOD_INITIALIZE, initialize_params(), timeout=self.timeout, reinitialize=False
            )
            self._finish_handshake(result)
            await self._notify(METHOD_INITIALIZED)
        except MCPConnectError:
            raise
        except MCPError as exc:
            raise MCPConnectError(f"handshake failed: {exc}") from None

    async def _rpc(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout: float,
        reinitialize: bool = True,
    ) -> Any:
        self._next_id += 1
        request_id = self._next_id
        message = make_request(request_id, method, params)
        try:
            response = await self._post(message, expect_id=request_id, timeout=timeout)
        except MCPSessionExpired:
            if not reinitialize:
                raise
            # The server forgot our session (restart, expiry): one transparent
            # re-handshake, then the original request again.
            await self.connect()
            response = await self._post(message, expect_id=request_id, timeout=timeout)
        if response is None:
            raise MCPTransportError(f"{method}: server accepted the request but sent no response")
        error = response.get("error")
        if isinstance(error, dict):
            raise MCPRemoteError(
                int(error.get("code", -32000)),
                str(error.get("message", "unknown error")),
                error.get("data"),
            )
        return response.get("result")

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._post(make_notification(method, params), expect_id=None, timeout=self.timeout)

    async def _post(
        self, message: dict[str, Any], *, expect_id: int | None, timeout: float
    ) -> dict[str, Any] | None:
        if self._closed:
            raise MCPTransportError(f"mcp client '{self.server_name}' is closed")
        verdict = self._enforce_policy()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._resolve_headers(),
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        body = encode_message(message)[:-1]
        self.requests_sent += 1
        try:
            async with (
                httpx.AsyncClient(
                    timeout=timeout,
                    transport=PinnedAsyncHTTPTransport(verdict, transport=self.transport),
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream("POST", self.url, headers=headers, content=body) as response,
            ):
                status = response.status_code
                if status == 404 and self.session_id:
                    raise MCPSessionExpired("server no longer knows this session")
                if status in (202, 204):
                    return None
                if status >= 300:
                    snippet = (await self._read_capped(response))[:200].decode(
                        "utf-8", errors="replace"
                    )
                    raise MCPTransportError(
                        f"HTTP {status} from MCP endpoint: {scrub_credentials(snippet).strip()}"
                    )
                session = response.headers.get("mcp-session-id")
                if session:
                    self.session_id = session
                content_type = (
                    response.headers.get("content-type", "").split(";")[0].strip().lower()
                )
                payload = await self._read_capped(response)
        except httpx.TimeoutException as exc:
            raise MCPTimeoutError(f"{message.get('method', 'request')} timed out: {exc}") from None
        except httpx.HTTPError as exc:
            raise MCPTransportError(
                scrub_credentials(f"{type(exc).__name__}: {exc}") or type(exc).__name__
            ) from None

        if expect_id is None:
            return None
        text = payload.decode("utf-8", errors="replace")
        if content_type == "application/json":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise MCPProtocolError(f"invalid JSON response: {exc.msg}") from None
            candidates = parsed if isinstance(parsed, list) else [parsed]
            for candidate in candidates:
                if isinstance(candidate, dict) and candidate.get("id") == expect_id:
                    return candidate
            raise MCPProtocolError("response did not carry our request id")
        if content_type == "text/event-stream":
            replies: list[dict[str, Any]] = []
            answer: dict[str, Any] | None = None
            for event in parse_sse(text):
                if classify_message(event) == "response":
                    if event.get("id") == expect_id:
                        answer = event
                    continue
                reply = self._handle_incoming(event)
                if reply is not None:
                    replies.append(reply)
            for reply in replies:
                with contextlib.suppress(MCPError):
                    await self._post(reply, expect_id=None, timeout=timeout)
            if answer is None:
                raise MCPProtocolError("SSE stream ended without our response")
            return answer
        raise MCPProtocolError(f"unexpected content-type {content_type or '(none)'!r}")

    async def _read_capped(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self.max_body_bytes:
                raise MCPTransportError(f"response body exceeded {self.max_body_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.connected = False
        if not self.session_id:
            return
        # Best-effort session teardown; a server that does not support DELETE
        # answers 405 and that is fine.
        try:
            verdict = self._enforce_policy()
            headers = {**self._resolve_headers(), "Mcp-Session-Id": self.session_id}
            async with httpx.AsyncClient(
                timeout=min(self.timeout, 10.0),
                transport=PinnedAsyncHTTPTransport(verdict, transport=self.transport),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                await client.delete(self.url, headers=headers)
        except Exception:
            pass
        finally:
            self.session_id = None


__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_MAX_LINE_BYTES",
    "SANDBOX_MARKER_ENV",
    "BaseMCPClient",
    "HttpMCPClient",
    "StdioMCPClient",
    "policy_for_endpoint",
]
