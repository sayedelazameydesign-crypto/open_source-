"""SSRF guard — the network egress boundary.

Section 5.2 of the architecture doc: the sandbox must never reach loopback,
link-local, cloud metadata or RFC1918 space. This module is *the* allow/deny
decision point for every outbound URL, used by the browser tool, the egress
policy and the reach layer. Note that every DNS answer is resolved and
validated before the verdict, which closes the rebinding hole only if the
caller pins the approved set (``connect_ip`` and ``connect_ips``).

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
    # All DNS answers are validated before any is tried; the pinned transport
    # may fall back between these approved addresses without resolving again.
    connect_ips: list[str] = Field(default_factory=list, exclude=True)

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

    def check(self, url: str, *, resolve_dns: bool | None = None) -> URLVerdict:
        """Check a URL and, when requested, return every approved IP to try.

        ``resolve_dns`` overrides the guard's configured default for this
        decision. Network callers should resolve names and pass ``connect_ip``
        plus the full ``connect_ips`` set to a pinned transport; dry-run policy
        checks may leave DNS disabled.
        """
        try:
            parsed = urlparse(url.strip())
            port = parsed.port
            scheme = (parsed.scheme or "").lower()
            host = (parsed.hostname or "").lower().rstrip(".")
        except (AttributeError, TypeError, ValueError) as exc:
            return self._deny(str(url), "", f"malformed URL: {exc}")

        if parsed.username is not None or parsed.password is not None:
            return self._deny(url, host, "credentials embedded in URL are not permitted")
        if port == 0:
            return self._deny(url, host, "port must be in 1..65535")
        if not scheme:
            return self._deny(url, host, "missing scheme (use http:// or https://)")
        if scheme not in self.allow_schemes:
            return self._deny(url, host, f"scheme '{scheme}' is not permitted")
        if not host:
            return self._deny(url, host, "missing host")
        blocked_hosts = {item.lower().rstrip(".") for item in self.blocked_hosts}
        extra_blocked = {item.lower().rstrip(".") for item in self.extra_blocked_hosts}
        if host in blocked_hosts or host in extra_blocked:
            return self._deny(url, host, f"host '{host}' is on the deny list")
        allowed_hosts = (
            {item.lower().rstrip(".") for item in self.allowed_hosts}
            if self.allowed_hosts is not None
            else None
        )
        if allowed_hosts is not None and host not in allowed_hosts:
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

        should_resolve = self.resolve_dns if resolve_dns is None else resolve_dns
        if not should_resolve:
            return URLVerdict(
                allowed=True, url=url, host=host, reason="host allowed (DNS not resolved)"
            )

        try:
            infos = socket.getaddrinfo(host, None)
        except OSError as exc:
            return self._deny(url, host, f"DNS resolution failed: {exc}")
        if not infos:
            return self._deny(url, host, "DNS resolution failed: no addresses returned")

        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except (IndexError, TypeError, ValueError) as exc:
                return self._deny(url, host, f"DNS resolution returned an invalid address: {exc}")
            if not self._ip_ok(ip):
                return self._deny(
                    url,
                    host,
                    f"host resolves to {ip} which is {self._ip_reason(ip)} — "
                    "possible SSRF/DNS rebinding",
                    connect_ip=str(ip),
                )
            addresses.append(ip)
        return URLVerdict(
            allowed=True,
            url=url,
            host=host,
            connect_ip=addresses[0].compressed,
            connect_ips=list(dict.fromkeys(address.compressed for address in addresses)),
            reason="all resolved addresses are public",
        )

    # -- internals ------------------------------------------------------
    def _verdict_for_ip(
        self, url: str, host: str, ip: ipaddress.IPv4Address | ipaddress.IPv6Address
    ) -> URLVerdict:
        if self._is_blocked_ip(ip):
            return self._deny(
                url, host, f"{ip} is a cloud metadata endpoint", connect_ip=ip.compressed
            )
        if not self._ip_ok(ip):
            return self._deny(url, host, f"{ip} is {self._ip_reason(ip)}", connect_ip=ip.compressed)
        return URLVerdict(
            allowed=True,
            url=url,
            host=host,
            connect_ip=ip.compressed,
            connect_ips=[ip.compressed],
            reason="public address",
        )

    @staticmethod
    def _effective_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
        """Treat IPv4-mapped IPv6 addresses as their IPv4 destination."""
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        return ip

    def _is_blocked_ip(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        effective = self._effective_ip(ip)
        if effective.compressed in self.blocked_ips:
            return True
        for blocked in self.blocked_ips:
            try:
                if self._effective_ip(ipaddress.ip_address(blocked)) == effective:
                    return True
            except ValueError:
                continue
        return False

    def _ip_ok(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        if self._is_blocked_ip(ip):
            return False
        candidate = self._effective_ip(ip)
        if self.allow_private:
            return not (
                candidate.is_loopback
                or candidate.is_link_local
                or candidate.is_multicast
                or candidate.is_unspecified
                or candidate.is_reserved
            )
        # is_global is deliberately stricter than "not RFC1918": it also
        # excludes special-use ranges such as shared-address space that are
        # neither private nor globally routable. Keep the explicit checks too:
        # some Python versions report multicast as globally scoped.
        return candidate.is_global and not (
            candidate.is_loopback
            or candidate.is_link_local
            or candidate.is_reserved
            or candidate.is_multicast
            or candidate.is_unspecified
        )

    def _ip_reason(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
        candidate = self._effective_ip(ip)
        for flag, label in (
            ("is_loopback", "loopback"),
            ("is_link_local", "link-local (cloud metadata range)"),
            ("is_private", "private/RFC1918"),
            ("is_reserved", "reserved"),
            ("is_multicast", "multicast"),
            ("is_unspecified", "unspecified"),
        ):
            if getattr(candidate, flag):
                return label
        return "non-public or blocked by policy"

    def _deny(self, url: str, host: str, reason: str, connect_ip: str | None = None) -> URLVerdict:
        return URLVerdict(allowed=False, url=url, host=host, reason=reason, connect_ip=connect_ip)
