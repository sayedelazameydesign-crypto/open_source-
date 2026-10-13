"""Explicit network egress policy — default-deny allow-list over the SSRF floor.

``SSRFGuard`` establishes the non-negotiable network boundary (no loopback,
RFC1918, link-local, multicast or cloud-metadata targets). :class:`EgressPolicy`
layers an explicit allow/deny rule set on top:

1. **Syntax check** — the URL must be ``http`` or ``https`` with a hostname and
   a valid port (``parse_http_target``).
2. **Network boundary floor** — :class:`SSRFGuard` runs first. No allow rule
   (not even ``host="*"``) can ever override a boundary denial.
3. **Explicit deny rules** — evaluated before allow rules so a broad allow
   (e.g. ``*.example.org``) can carve out a forbidden subdomain
   (e.g. ``admin.example.org``).
4. **Explicit allow rules** — scoped by host pattern, scheme, port and tool.
5. **Default deny** — if no allow rule matches, egress is refused with
   ``no allow rule permits <scheme>://<host>:<port> for '<tool>'``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from os_tools.ssrf import BlockedURLError, SSRFGuard, URLVerdict

DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


class EgressPolicyError(BlockedURLError):
    """Raised when an outbound URL is rejected by the egress policy."""


def parse_http_target(url: str) -> tuple[str, str, int]:
    """Validate and normalise ``url`` into ``(scheme, host, port)``.

    Raises :class:`ValueError` when ``url`` is not an ``http(s)`` URL with a
    valid hostname and port.
    """
    if not isinstance(url, str):
        raise ValueError(f"{url!r} is not an http(s) URL with a host")
    cleaned = url.strip()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        raise ValueError(f"{url!r} is not an http(s) URL with a host")
    try:
        parsed = urlparse(cleaned)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{url!r} is not an http(s) URL with a host") from exc

    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if scheme not in DEFAULT_PORTS or not host:
        raise ValueError(f"{url!r} is not an http(s) URL with a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{url!r} is not an http(s) URL with a host")

    resolved_port = port if port is not None else DEFAULT_PORTS[scheme]
    if not (1 <= resolved_port <= 65535):
        raise ValueError(f"{url!r} is not an http(s) URL with a host")
    return scheme, host, resolved_port


def _host_matches(pattern: str, host: str) -> bool:
    pat = pattern.lower().strip().rstrip(".")
    candidate = host.lower().strip().rstrip(".")
    if not pat or not candidate:
        return False
    if pat == "*":
        return True
    if pat.startswith("*."):
        suffix = pat[2:]
        if not suffix or candidate == suffix:
            return False
        return candidate.endswith(f".{suffix}")
    return candidate == pat


class EgressRule(BaseModel):
    """One allow or deny rule for outbound traffic."""

    action: Literal["allow", "deny"] = "allow"
    host: str
    schemes: set[str] = Field(default_factory=lambda: {"http", "https"})
    ports: set[int] = Field(default_factory=lambda: {80, 443})
    tools: set[str] | None = None
    description: str = ""

    @field_validator("host")
    @classmethod
    def _normalise_host(cls, value: str) -> str:
        raw = value.strip().lower()
        if raw.startswith("*.") and not raw[2:].rstrip("."):
            raise ValueError("wildcard egress rule must name a base domain (e.g. '*.example.org')")
        cleaned = raw.rstrip(".")
        if not cleaned or any(ch.isspace() for ch in cleaned):
            raise ValueError("egress rule host pattern must be non-empty")
        if "*" in cleaned and cleaned != "*" and not cleaned.startswith("*."):
            raise ValueError("wildcards are only supported as '*' or '*.domain'")
        if cleaned.startswith("*.") and "*" in cleaned[2:]:
            raise ValueError("nested wildcards are not supported in host pattern")
        return cleaned

    @field_validator("schemes")
    @classmethod
    def _normalise_schemes(cls, value: set[str]) -> set[str]:
        normalised = {s.strip().lower() for s in value if s.strip()}
        if not normalised or not normalised.issubset(DEFAULT_PORTS):
            raise ValueError("egress rule schemes must be a non-empty subset of {'http', 'https'}")
        return normalised

    @field_validator("ports")
    @classmethod
    def _validate_ports(cls, value: set[int]) -> set[int]:
        if not value or any(p < 1 or p > 65535 for p in value):
            raise ValueError("egress rule ports must be in 1..65535")
        return set(value)

    def matches(self, *, scheme: str, host: str, port: int, tool: str) -> bool:
        if scheme.lower() not in self.schemes:
            return False
        if port not in self.ports:
            return False
        if self.tools is not None and tool not in self.tools:
            return False
        return _host_matches(self.host, host)


class EgressVerdict(URLVerdict):
    """Decision returned by :meth:`EgressPolicy.check`."""

    scheme: str = ""
    port: int = 443
    tool: str = "browser"
    matched_rule: str | None = None

    def raise_if_blocked(self) -> None:
        if not self.allowed:
            raise EgressPolicyError(self.reason)

    def raise_if_denied(self) -> None:
        self.raise_if_blocked()


class EgressPolicy(BaseModel):
    """Default-deny outbound policy backed by an un-overridable :class:`SSRFGuard`."""

    rules: list[EgressRule] = Field(default_factory=list)
    guard: SSRFGuard = Field(default_factory=lambda: SSRFGuard(resolve_dns=False))
    default_tool: str = "browser"
    audit_log: list[EgressVerdict] = Field(default_factory=list)

    @classmethod
    def allow_hosts(
        cls,
        *hosts: str,
        schemes: set[str] | None = None,
        ports: set[int] | None = None,
        tools: set[str] | None = None,
        resolve_dns: bool = False,
        default_tool: str = "browser",
    ) -> EgressPolicy:
        rule_schemes = schemes if schemes is not None else {"http", "https"}
        rule_ports = ports if ports is not None else {80, 443}
        rules = [
            EgressRule(
                action="allow",
                host=h,
                schemes=set(rule_schemes),
                ports=set(rule_ports),
                tools=set(tools) if tools is not None else None,
            )
            for h in hosts
        ]
        return cls(
            rules=rules,
            guard=SSRFGuard(resolve_dns=resolve_dns),
            default_tool=default_tool,
        )

    def allowed_hosts_summary(self, *, tool: str | None = None) -> list[str]:
        active_tool = tool or self.default_tool
        seen: list[str] = []
        for rule in self.rules:
            if rule.action != "allow":
                continue
            if rule.tools is not None and active_tool not in rule.tools:
                continue
            if rule.host not in seen:
                seen.append(rule.host)
        return seen

    def check(self, url: str, *, tool: str | None = None) -> EgressVerdict:
        active_tool = tool or self.default_tool
        try:
            scheme, host, port = parse_http_target(url)
        except ValueError as exc:
            verdict = EgressVerdict(
                allowed=False,
                url=url,
                host="",
                scheme="",
                port=0,
                tool=active_tool,
                reason=str(exc),
            )
            self.audit_log.append(verdict)
            return verdict

        boundary = self.guard.check(url)
        if not boundary.allowed:
            verdict = EgressVerdict(
                allowed=False,
                url=url,
                host=host,
                scheme=scheme,
                port=port,
                tool=active_tool,
                connect_ip=boundary.connect_ip,
                reason=f"network boundary: {boundary.reason}",
            )
            self.audit_log.append(verdict)
            return verdict

        origin = f"{scheme}://{host}:{port}"

        for rule in self.rules:
            if rule.action == "deny" and rule.matches(
                scheme=scheme, host=host, port=port, tool=active_tool
            ):
                verdict = EgressVerdict(
                    allowed=False,
                    url=url,
                    host=host,
                    scheme=scheme,
                    port=port,
                    tool=active_tool,
                    connect_ip=boundary.connect_ip,
                    matched_rule=f"deny:{rule.host}",
                    reason=f"deny rule '{rule.host}' blocks {origin} for {active_tool!r}",
                )
                self.audit_log.append(verdict)
                return verdict

        for rule in self.rules:
            if rule.action == "allow" and rule.matches(
                scheme=scheme, host=host, port=port, tool=active_tool
            ):
                verdict = EgressVerdict(
                    allowed=True,
                    url=url,
                    host=host,
                    scheme=scheme,
                    port=port,
                    tool=active_tool,
                    connect_ip=boundary.connect_ip,
                    matched_rule=f"allow:{rule.host}",
                    reason=f"allow rule '{rule.host}' permits {origin} for {active_tool!r}",
                )
                self.audit_log.append(verdict)
                return verdict

        verdict = EgressVerdict(
            allowed=False,
            url=url,
            host=host,
            scheme=scheme,
            port=port,
            tool=active_tool,
            connect_ip=boundary.connect_ip,
            reason=f"no allow rule permits {origin} for {active_tool!r}",
        )
        self.audit_log.append(verdict)
        return verdict

    def enforce(self, url: str, *, tool: str | None = None) -> EgressVerdict:
        """Validate ``url`` syntax and raise :class:`EgressPolicyError` if denied."""
        parse_http_target(url)
        verdict = self.check(url, tool=tool)
        verdict.raise_if_denied()
        return verdict

    def for_tool(self, tool: str) -> Callable[[str], EgressVerdict]:
        """Return a single-argument check suitable for ``ssrf_check`` hooks."""
        return lambda url: self.check(url, tool=tool)
