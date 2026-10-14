"""MCP server configuration — the ecosystem's ``mcpServers`` file, plus our policy.

The file format is the one every MCP host already uses (Claude Desktop,
Claude Code ``.mcp.json``, Cursor, Windsurf …)::

    {
      "mcpServers": {
        "filesystem": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/srv/work"],
          "env": {"HOME": "${HOME}"},
          "risk": "write",
          "tools": {"allow": ["read_file", "list_directory"]},
          "timeout": 30
        },
        "docs": {
          "url": "https://mcp.example.com/mcp",
          "headers": {"Authorization": "Bearer ${DOCS_MCP_TOKEN}"},
          "risk": "network"
        }
      }
    }

so an operator can point ``CELA_MCP_CONFIG`` at a file they already have. The
keys this platform adds (``risk``, ``tool_risk``, ``tools``, ``timeout``,
``max_chars``) are all *policy*, and all of them have a fail-closed default:

* ``risk`` defaults to ``dangerous`` — every call to a server the operator has
  not reviewed goes through HITL. Lowering it is a one-line, per-server
  decision taken after reading what the server does.
* ``${VAR}`` references in ``env`` and ``headers`` are resolved from the
  process environment *at use time*; a missing variable is an error, never an
  empty string (ADR-0011: secrets live in the environment, not in files).
* The config file itself must not be writable by anyone but its owner:
  whoever can edit ``command`` can run arbitrary code as the server, so a
  group- or world-writable file is refused outright.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from os_core.types import RiskLevel
from os_tools.egress import parse_http_target
from pydantic import BaseModel, Field, field_validator, model_validator

from os_mcp.protocol import MCPError

MCP_ENABLE_ENV = "CELA_MCP_ENABLE"
MCP_CONFIG_ENV = "CELA_MCP_CONFIG"
#: Second, separate opt-in for *stdio* servers. ``CELA_MCP_ENABLE=1`` only says
#: "read the config and talk to MCP servers"; a stdio server is additionally a
#: process the platform did not write, started on the host OUTSIDE the sandbox
#: and outside ``EgressPolicy`` — that needs its own, explicit yes.
MCP_HOST_EXEC_ENV = "CELA_MCP_ALLOW_HOST_EXEC"
HOST_EXEC_DISABLED_REASON = (
    "stdio server would run as a process on the host, outside the sandbox and the egress "
    f"policy; set {MCP_HOST_EXEC_ENV}=1 to allow host execution"
)
CANONICAL_CONFIG_PATH = "~/.config/cela/mcp.json"

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_CHARS = 20_000
MAX_TIMEOUT = 600.0

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: Environment a stdio server inherits from us when the config says nothing.
#: Deliberately small (same list the reach CLI adapter uses): PATH to find
#: binaries, locale/time for well-behaved tools, temp dirs for scratch files.
#: None of the server's own secrets (API tokens, model keys) reach the child
#: unless the operator names them in ``env``.
DEFAULT_ENV_ALLOW: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "WINDIR",
)


class MCPConfigError(MCPError):
    """The configuration cannot be used as written."""


def expand_env(template: str, env: Mapping[str, str] | None = None) -> str:
    """Replace every ``${VAR}`` in *template* from *env* (default: ``os.environ``).

    A reference to an unset variable raises :class:`MCPConfigError` — a header
    that silently becomes ``Bearer `` is a worse failure than a loud one.
    """
    source = os.environ if env is None else env

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        value = source.get(name)
        if value is None:
            raise MCPConfigError(f"environment variable {name} is not set")
        return value

    return _ENV_REF_RE.sub(_sub, template)


def references_env(template: str) -> list[str]:
    return _ENV_REF_RE.findall(template)


class MCPToolFilter(BaseModel):
    """Allow/deny list over a server's tool names (the *server-side* names)."""

    allow: list[str] | None = None
    deny: list[str] = Field(default_factory=list)

    def permits(self, name: str) -> bool:
        if name in self.deny:
            return False
        return self.allow is None or name in self.allow


class MCPServerConfig(BaseModel):
    """One server entry: exactly one transport, plus policy."""

    name: str
    # stdio transport
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    # streamable HTTP transport
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    # policy
    risk: RiskLevel = RiskLevel.DANGEROUS
    tool_risk: dict[str, RiskLevel] = Field(default_factory=dict)
    tools: MCPToolFilter = Field(default_factory=MCPToolFilter)
    timeout: float = DEFAULT_TIMEOUT
    max_chars: int = DEFAULT_MAX_CHARS
    enabled: bool = True
    disabled_reason: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _SERVER_NAME_RE.match(value):
            raise ValueError(
                "server name must be 1-32 characters of [A-Za-z0-9_-] starting alphanumeric"
            )
        return value

    @field_validator("timeout")
    @classmethod
    def _check_timeout(cls, value: float) -> float:
        if not (1.0 <= value <= MAX_TIMEOUT):
            raise ValueError(f"timeout must be between 1 and {MAX_TIMEOUT:.0f} seconds")
        return float(value)

    @field_validator("max_chars")
    @classmethod
    def _check_max_chars(cls, value: int) -> int:
        if value < 256:
            raise ValueError("max_chars must be at least 256")
        return int(value)

    @field_validator("args")
    @classmethod
    def _check_args(cls, value: list[str]) -> list[str]:
        for item in value:
            if not isinstance(item, str):
                raise ValueError("args must be strings")
            if "\x00" in item or "\n" in item or "\r" in item:
                raise ValueError("args must not contain NUL or newline characters")
        return list(value)

    @model_validator(mode="after")
    def _one_transport(self) -> MCPServerConfig:
        has_command = bool(self.command)
        has_url = bool(self.url)
        if has_command == has_url:
            raise ValueError("exactly one of 'command' (stdio) or 'url' (http) is required")
        if has_command and ("\x00" in self.command or "\n" in self.command):  # type: ignore[operator]
            raise ValueError("command must not contain NUL or newline characters")
        if has_url:
            try:
                parse_http_target(self.url)  # type: ignore[arg-type]
            except ValueError as exc:
                raise ValueError(f"url: {exc}") from None
        return self

    @property
    def transport(self) -> Literal["stdio", "http"]:
        return "stdio" if self.command else "http"

    def risk_for(self, tool_name: str) -> RiskLevel:
        return self.tool_risk.get(tool_name, self.risk)

    def child_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Environment for a stdio server: allow-list + explicit ``env`` entries.

        ``${VAR}`` references are expanded from *base* (``os.environ`` by
        default) now, which is the moment the process is spawned — not when
        the file was read.
        """
        source = os.environ if base is None else base
        child = {key: source[key] for key in DEFAULT_ENV_ALLOW if key in source}
        for key, template in self.env.items():
            child[key] = expand_env(template, source)
        return child

    def resolved_headers(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Headers for an HTTP server with ``${VAR}`` expanded at request time."""
        return {key: expand_env(value, base) for key, value in self.headers.items()}

    def describe(self) -> dict[str, Any]:
        """Operator-facing summary; never includes resolved secrets."""
        target = self.command if self.command else self.url
        return {
            "name": self.name,
            "transport": self.transport,
            "target": target,
            "args": list(self.args),
            "risk": self.risk.value,
            "tool_risk": {k: v.value for k, v in self.tool_risk.items()},
            "allow": self.tools.allow,
            "deny": list(self.tools.deny),
            "timeout": self.timeout,
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "env_refs": sorted(
                {
                    ref
                    for value in list(self.env.values()) + list(self.headers.values())
                    for ref in references_env(value)
                }
            ),
        }


class MCPConfig(BaseModel):
    """The parsed file: servers in file order, plus notes about what was ignored."""

    servers: list[MCPServerConfig] = Field(default_factory=list)
    path: str | None = None
    notes: list[str] = Field(default_factory=list)

    def enabled_servers(self) -> list[MCPServerConfig]:
        return [s for s in self.servers if s.enabled]

    def get(self, name: str) -> MCPServerConfig | None:
        for server in self.servers:
            if server.name == name:
                return server
        return None


_KNOWN_KEYS = {
    "command",
    "args",
    "env",
    "cwd",
    "url",
    "headers",
    "risk",
    "tool_risk",
    "tools",
    "timeout",
    "max_chars",
    "enabled",
    "disabled",
    "type",
    "transport",
}


def host_exec_allowed(env: Mapping[str, str] | None = None) -> bool:
    """``True`` only when the operator set ``CELA_MCP_ALLOW_HOST_EXEC=1``."""
    source = os.environ if env is None else env
    return source.get(MCP_HOST_EXEC_ENV) == "1"


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    raw = source.get(MCP_CONFIG_ENV) or CANONICAL_CONFIG_PATH
    return Path(raw).expanduser()


def _check_permissions(path: Path) -> None:
    """Refuse a config that someone other than its owner can edit (POSIX)."""
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise MCPConfigError(f"cannot stat {path}: {exc}") from None
    if os.name != "posix":  # pragma: no cover — Windows ACLs are not mode bits
        return
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise MCPConfigError(
            f"{path} is writable by group/others (mode {stat.S_IMODE(mode):04o}); whoever can "
            "edit this file can run arbitrary commands as an MCP server — chmod 600 it"
        )


def parse_mcp_config(payload: Any, *, allow_host_exec: bool, path: str | None = None) -> MCPConfig:
    """Build an :class:`MCPConfig` from the decoded JSON document.

    *allow_host_exec* is deliberately required: without it every stdio entry
    is parsed, kept, and marked ``enabled=False`` with
    :data:`HOST_EXEC_DISABLED_REASON`, so ``cela mcp list`` shows exactly what
    would run and why it does not. HTTP entries are unaffected.
    """
    if not isinstance(payload, dict):
        raise MCPConfigError("config root must be a JSON object")
    raw_servers = payload.get("mcpServers")
    if raw_servers is None:
        raw_servers = payload.get("servers")
    if raw_servers is None:
        raise MCPConfigError("config needs an 'mcpServers' (or 'servers') object")
    if not isinstance(raw_servers, dict):
        raise MCPConfigError("'mcpServers' must be an object keyed by server name")

    config = MCPConfig(path=path)
    held_back: list[str] = []
    for name, raw in raw_servers.items():
        if not isinstance(raw, dict):
            raise MCPConfigError(f"server {name!r}: entry must be an object")
        unknown = sorted(set(raw) - _KNOWN_KEYS)
        if unknown:
            config.notes.append(f"{name}: ignored unknown keys {', '.join(unknown)}")
        data: dict[str, Any] = {"name": name}
        for key in ("command", "args", "env", "cwd", "url", "headers", "risk", "timeout"):
            if key in raw:
                data[key] = raw[key]
        if "tool_risk" in raw:
            data["tool_risk"] = raw["tool_risk"]
        if "max_chars" in raw:
            data["max_chars"] = raw["max_chars"]
        tools = raw.get("tools")
        if isinstance(tools, dict):
            data["tools"] = tools
        elif isinstance(tools, list):
            data["tools"] = {"allow": tools}
        enabled = bool(raw.get("enabled", True)) and not bool(raw.get("disabled", False))
        data["enabled"] = enabled
        if not enabled:
            data["disabled_reason"] = "disabled in config"
        declared_type = str(raw.get("type") or raw.get("transport") or "").lower()
        if declared_type == "sse":
            # Legacy HTTP+SSE (2024-11-05) needs a second endpoint discovered
            # over a GET stream; it is not implemented. Say so instead of
            # trying Streamable HTTP against it and reporting a confusing 4xx.
            data["enabled"] = False
            data["disabled_reason"] = (
                "legacy 'sse' transport is not supported; use a Streamable HTTP endpoint "
                "or run the server over stdio"
            )
        try:
            server = MCPServerConfig(**data)
        except ValueError as exc:
            raise MCPConfigError(f"server {name!r}: {exc}") from None
        if server.transport == "stdio" and server.enabled and not allow_host_exec:
            server = server.model_copy(
                update={"enabled": False, "disabled_reason": HOST_EXEC_DISABLED_REASON}
            )
            held_back.append(name)
        config.servers.append(server)
    if held_back:
        config.notes.append(
            f"{len(held_back)} stdio server(s) held back ({', '.join(held_back)}): "
            f"{HOST_EXEC_DISABLED_REASON}"
        )
    return config


def load_mcp_config(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    allow_host_exec: bool | None = None,
) -> MCPConfig:
    """Read and validate the config file at *path* (default: ``CELA_MCP_CONFIG``).

    A missing file is not an error: it yields an empty config with a note, so
    ``cela mcp list`` can explain where the file is expected. stdio servers
    stay disabled unless *allow_host_exec* is true — by default that is read
    from ``CELA_MCP_ALLOW_HOST_EXEC`` in *env*.
    """
    if allow_host_exec is None:
        allow_host_exec = host_exec_allowed(env)
    target = Path(path).expanduser() if path is not None else default_config_path(env)
    if not target.exists():
        return MCPConfig(path=str(target), notes=[f"no config file at {target}"])
    _check_permissions(target)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MCPConfigError(f"cannot read {target}: {exc}") from None
    return parse_mcp_config(payload, allow_host_exec=allow_host_exec, path=str(target))


__all__ = [
    "CANONICAL_CONFIG_PATH",
    "DEFAULT_ENV_ALLOW",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_TIMEOUT",
    "HOST_EXEC_DISABLED_REASON",
    "MCP_CONFIG_ENV",
    "MCP_ENABLE_ENV",
    "MCP_HOST_EXEC_ENV",
    "MCPConfig",
    "MCPConfigError",
    "MCPServerConfig",
    "MCPToolFilter",
    "default_config_path",
    "expand_env",
    "host_exec_allowed",
    "load_mcp_config",
    "parse_mcp_config",
    "references_env",
]
