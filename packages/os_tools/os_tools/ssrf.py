"""SSRF guard — the network egress boundary.

Section 5.2 of the architecture doc: the sandbox must never reach loopback,
link-local, cloud metadata or RFC1918 space. This module is *the* allow/deny
decision point for every outbound URL, used by the browser tool, the egress
policy and the reach layer. Note that DNS is resolved before the verdict,
which closes the rebinding hole only if the caller pins the resolved IP
(``connect_ip``).

Two host-shaped bypasses are refused outright, because both slip past an IP
check that only ever sees *canonical* literals (hardening ported from
Agent-Reach's ``normalize_public_http_url``, ADR-0013):

* **Ambiguous numeric spellings** — ``http://2130706433/``, ``http://127.1/``
  and ``http://0x7f.0.0.1/`` all mean loopback while ``ipaddress`` reads them
  as hostnames, so the DNS/IP verdict never fires.
* **Single-label hosts** — ``http://intranet/`` resolves inside the network the
  guard is trying to protect, and with ``resolve_dns=False`` it is not even
  resolved. Internal service names are opt-in via ``allow_single_label_hosts``
  (the honest escape hatch beats a silent hole).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from pydantic import BaseModel, Field

# Schemes a browser/HTTP tool may ever use.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Hostnames that must never be reached from a sandbox.
BLOCKED_HOSTS: frozenset[str] = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata.internal",
        "instance-data",
        "kubernetes.default.svc",
    }
)

# Cloud metadata IPs, hardcoded so a bad DNS answer cannot smuggle them in.
BLOCKED_IPS: frozenset[str] = frozenset({"169.254.169.254", "fd00:ec2::254", "100.100.100.200"})


class URLVerdict(BaseModel):
    allowed: bool
    url: str
    reason: str
    host: str = ""
    connect_ip: str | None = None

    def raise_if_blocked(self) -> None:
        if not self.allowed:
            raise BlockedURLError(self.reason)


class BlockedURLError(RuntimeError):
    pass


def _canonical_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the address when *host* is a canonical IP literal, else None."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _ambiguous_numeric_host(host: str) -> str | None:
    """Return the address a legacy numeric spelling means, if this is one.

    ``inet_aton`` accepts forms ``ipaddress`` rejects (``2130706433``,
    ``127.1``, ``0x7f.0.0.1``). Every one of them is an address in disguise —
    nothing legitimate reaches the internet through one — so the guard refuses
    them instead of guessing.
    """
    if _canonical_ip_literal(host) is not None:
        return None
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return None
    return str(ipaddress.IPv4Address(packed))


class SSRFGuard(BaseModel):
    """Policy object: allow public internet, deny everything internal."""

    allow_private: bool = False
    allow_schemes: set[str] = Field(default_factory=lambda: set(ALLOWED_SCHEMES))
    blocked_hosts: set[str] = Field(default_factory=lambda: set(BLOCKED_HOSTS))
    blocked_ips: set[str] = Field(default_factory=lambda: set(BLOCKED_IPS))
    extra_blocked_hosts: set[str] = Field(default_factory=set)
    allowed_hosts: set[str] | None = None  # if set, allowlist mode
    # Internal service names ("http://intranet/") are single-label: refused
    # unless an operator opts in, because with resolve_dns=False they are not
    # resolved and with DNS they resolve inside the very network this guard
    # exists to protect. Opting in is a written decision, not a silent default.
    allow_single_label_hosts: bool = False
    resolve_dns: bool = True
    default_port: int = 443

    def check(self, url: str) -> URLVerdict:
        parsed = urlparse(url.strip())
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()

        if not scheme:
            return self._deny(url, host, "missing scheme (use http:// or https://)")
        if scheme not in self.allow_schemes:
            return self._deny(url, host, f"scheme '{scheme}' is not permitted")
        if not host:
            return self._deny(url, host, "missing host")
        if host in self.blocked_hosts or host in self.extra_blocked_hosts:
            return self._deny(url, host, f"host '{host}' is on the deny list")
        if self.allowed_hosts is not None and host not in self.allowed_hosts:
            return self._deny(url, host, f"host '{host}' is not on the allow list")

        ambiguous = _ambiguous_numeric_host(host)
        if ambiguous is not None:
            return self._deny(
                url,
                host,
                f"numeric host '{host}' is an ambiguous spelling of {ambiguous}; "
                "only canonical IP literals are accepted",
                connect_ip=ambiguous,
            )

        # Literal IP in the URL?
        literal = _canonical_ip_literal(host)
        if literal is not None:
            return self._verdict_for_ip(url, host, literal)

        if not self.allow_single_label_hosts and "." not in host:
            return self._deny(
                url,
                host,
                f"host '{host}' is a single label (no domain part); internal service "
                "names need allow_single_label_hosts=True",
            )

        if not self.resolve_dns:
            return URLVerdict(
                allowed=True, url=url, host=host, reason="host allowed (DNS not resolved)"
            )

        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            return self._deny(url, host, f"DNS resolution failed: {exc}")

        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not self._ip_ok(ip):
                return self._deny(
                    url,
                    host,
                    f"host resolves to {ip} which is {self._ip_reason(ip)} — "
                    "possible SSRF/DNS rebinding",
                    connect_ip=str(ip),
                )
        first = str(ipaddress.ip_address(infos[0][4][0]))
        return URLVerdict(
            allowed=True,
            url=url,
            host=host,
            connect_ip=first,
            reason="all resolved addresses are public",
        )

    # -- internals ------------------------------------------------------
    def _verdict_for_ip(
        self, url: str, host: str, ip: ipaddress.IPv4Address | ipaddress.IPv6Address
    ) -> URLVerdict:
        if str(ip) in self.blocked_ips:
            return self._deny(url, host, f"{ip} is a cloud metadata endpoint", connect_ip=str(ip))
        if not self._ip_ok(ip):
            return self._deny(url, host, f"{ip} is {self._ip_reason(ip)}", connect_ip=str(ip))
        return URLVerdict(
            allowed=True, url=url, host=host, connect_ip=str(ip), reason="public address"
        )

    def _ip_ok(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        if str(ip) in self.blocked_ips:
            return False
        if self.allow_private:
            return not (ip.is_loopback or ip.is_link_local)
        return not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )

    def _ip_reason(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
        for flag, label in (
            ("is_loopback", "loopback"),
            ("is_link_local", "link-local (cloud metadata range)"),
            ("is_private", "private/RFC1918"),
            ("is_reserved", "reserved"),
            ("is_multicast", "multicast"),
            ("is_unspecified", "unspecified"),
        ):
            if getattr(ip, flag):
                return label
        return "blocked by policy"

    def _deny(self, url: str, host: str, reason: str, connect_ip: str | None = None) -> URLVerdict:
        return URLVerdict(allowed=False, url=url, host=host, reason=reason, connect_ip=connect_ip)
